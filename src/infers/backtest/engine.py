"""2パス式バックテストエンジン (設計書 §7.4 / §8)。

  Pass 1: collect_judgements() — L0決定論スイープで裁定イベントを全収集し、
          Batch API リクエストファイルを生成 (ai/batch.py)
  (外部)  Batch API 実行 → 結果JSONLを ingest_batch_results で VerdictCache へ
  Pass 2: run() — VerdictCache 参照のみで全期間をリプレイし、
          SimBroker 上で資産曲線・PF・最大DD・勝率・建値SL退出率を算出

ライブ同等性 (CLAUDE.md 第12条): 確定足処理は core/loop.py の TradingLoop に
集約されており、LiveRunner (execution/mt5_adapter.py) と同一コードパスを通る。
本モジュール固有なのは「SimBrokerの駆動」と「約定台帳による損益集計」のみ。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Iterable, Protocol

from infers.ai.gateway import (
    AiGateway, EscalationPolicy, JudgementRequest, Tier, VerdictCache, cache_key,
)
from infers.indicators import Q
from infers.core.loop import EquityProvider, ProviderOutput, SignalProvider, TradePlan, TradingLoop
from infers.core.models import Candle, SymbolSpec, Timeframe
from infers.execution.risk import RiskManager, VolumeSizer
from infers.execution.sim_broker import SimBroker
from infers.execution.sm import FsmConfig

__all__ = [
    "BacktestEngine", "BacktestEquityProvider", "BacktestReport", "LedgerBroker",
    "ProviderOutput", "SignalProvider", "SwapConfig", "TradePlan", "TradeRecord",
    "build_report", "compute_swap_tick_steps", "load_candles_parquet",
]


# ---------------------------------------------------------------------------
# 約定台帳つきブローカー (損益の集計用)
# ---------------------------------------------------------------------------

@dataclass
class _Ledger:
    direction: int = 0
    entries: list[tuple[int, int]] = field(default_factory=list)   # (price, volume)
    exits: list[tuple[int, int]] = field(default_factory=list)
    exit_kind: str = "CLOSE"          # 最終退出の種別: "SL" | "CLOSE"
    # 約定時刻 (entries/exits と同順の並行リスト。レポート可視化用)
    entry_times: list[datetime] = field(default_factory=list)
    exit_times: list[datetime] = field(default_factory=list)


class LedgerBroker(SimBroker):
    """SimBroker に約定台帳を重ねる (FSMからは通常の BrokerPort に見える)。"""

    def __init__(self, *, spread_ticks: int, min_stop_distance_ticks: int) -> None:
        super().__init__(spread_ticks=spread_ticks,
                         min_stop_distance_ticks=min_stop_distance_ticks)
        self.ledgers: dict[str, _Ledger] = {}
        self._bar_time: datetime | None = None   # 現在処理中バーの close_time

    def _ledger(self, position_id: str, direction: int) -> _Ledger:
        led = self.ledgers.setdefault(position_id, _Ledger())
        if led.direction == 0:
            led.direction = direction
        return led

    def _volume_of(self, position_id: str) -> int:
        pos = self.position(position_id)
        return pos.volume_steps if pos is not None else 0

    def place_limit(self, **kw) -> None:
        super().place_limit(**kw)
        # 方向を台帳に先行登録 (約定イベントは方向を運ばないため)
        self._ledger(kw["position_id"], kw["direction"])

    def place_market(self, **kw) -> int:
        before = self._volume_of(kw["position_id"])
        fill = super().place_market(**kw)
        if self._volume_of(kw["position_id"]) > before:   # 冪等再呼び出しは記録しない
            led = self._ledger(kw["position_id"], kw["direction"])
            led.entries.append((fill, kw["volume_steps"]))
            led.entry_times.append(self._bar_time)
        return fill

    def close_volume(self, **kw) -> int:
        before = self._volume_of(kw["position_id"])
        fill = super().close_volume(**kw)
        if self._volume_of(kw["position_id"]) < before:   # 冪等再呼び出しは記録しない
            led = self.ledgers.get(kw["position_id"])
            if led is not None:
                led.exits.append((fill, kw["volume_steps"]))
                led.exit_times.append(self._bar_time)
                led.exit_kind = "CLOSE"
        return fill

    def process_bar(self, candle: Candle):
        self._bar_time = candle.close_time
        events = super().process_bar(candle)
        for ev in events:
            led = self.ledgers.get(ev.position_id)
            if led is None:
                continue
            if ev.kind == "FILL":
                led.entries.append((ev.price_int, ev.volume_steps))
                led.entry_times.append(self._bar_time)
            elif ev.kind == "SL_HIT":
                led.exits.append((ev.price_int, ev.volume_steps))
                led.exit_times.append(self._bar_time)
                led.exit_kind = "SL"
        return events


# ---------------------------------------------------------------------------
# レポート
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# スワップ (オーバーナイト金利) モデル
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SwapConfig:
    """日跨ぎ保有コスト (設計書 §8 / コスト精緻化)。

    値は「1ステップ(=lot_step ロット)を1夜保有あたりのティック数」。負=コスト。
    USD建てのブローカー仕様からの換算は main.py (spec を知る層) で行う。
    保有数量は時間で変化する (打診→追撃→半分利確→ランナー) ため、数量×夜数を
    区間ごとに積分して課金する。
    """

    enabled: bool = False
    long_ticks_per_step: Decimal = Decimal(0)    # 買い保有: 1ステップ1夜あたりティック (負=コスト)
    short_ticks_per_step: Decimal = Decimal(0)   # 売り保有
    rollover_hour_utc: int = 21                   # ロールオーバー時刻 (UTC)
    triple_weekday: int = 2                       # 3倍課金曜日 (月=0..日=6, 既定=水)。-1で無効


def _weighted_nights(start: datetime, end: datetime, hour: int,
                     triple_weekday: int) -> int:
    """(start, end] の間に跨いだロールオーバー回数 (水曜は3倍)。"""
    if end <= start:
        return 0
    roll = start.replace(hour=hour, minute=0, second=0, microsecond=0)
    if roll <= start:
        roll += timedelta(days=1)
    total = 0
    while roll <= end:
        total += 3 if (triple_weekday >= 0 and roll.weekday() == triple_weekday) else 1
        roll += timedelta(days=1)
    return total


def compute_swap_tick_steps(direction: int, ledger: "_Ledger",
                            cfg: SwapConfig) -> int:
    """約定台帳の (数量×時間) タイムラインからスワップ総額 (tick*steps) を返す。

    entries=建玉(+)、exits=決済(-) を時刻順に並べ、区間ごとの保有数量に対し
    跨いだ夜数ぶんのスワップを課金する。負値=コスト。
    """
    if not cfg.enabled or not ledger.entry_times or not ledger.exit_times:
        return 0
    per_step = cfg.long_ticks_per_step if direction > 0 else cfg.short_ticks_per_step
    if per_step == 0:
        return 0
    events: list[tuple[datetime, int]] = []
    for (_p, v), t in zip(ledger.entries, ledger.entry_times):
        events.append((t, v))
    for (_p, v), t in zip(ledger.exits, ledger.exit_times):
        events.append((t, -v))
    events.sort(key=lambda e: e[0])

    total = Decimal(0)
    vol = 0
    prev_t = events[0][0]
    for t, dv in events:
        if vol > 0 and t > prev_t:
            nights = _weighted_nights(prev_t, t, cfg.rollover_hour_utc,
                                      cfg.triple_weekday)
            total += per_step * vol * nights
        vol += dv
        prev_t = t
    return int(total.to_integral_value(rounding=ROUND_HALF_EVEN))


# ---------------------------------------------------------------------------
# レポート
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TradeRecord:
    position_id: str
    direction: int
    pnl_tick_steps: int               # スワップ計上後の正味損益
    exit_kind: str                    # "SL" | "CLOSE"
    swap_tick_steps: int = 0          # うちスワップ分 (負=コスト)。透明性のため内訳保持

    @property
    def is_breakeven_sl_exit(self) -> bool:
        """建値SL退出: SL退出かつ損益が非負 (防御が機能した証跡)。"""
        return self.exit_kind == "SL" and self.pnl_tick_steps >= 0


@dataclass(frozen=True)
class BacktestReport:
    trades: tuple[TradeRecord, ...]
    equity_curve: tuple[int, ...]                 # トレード確定ごとの累積損益
    profit_factor: Decimal | None                 # 損失ゼロなら None
    win_rate: Decimal
    be_sl_exit_rate: Decimal                      # 建値SL退出率 (設計書 §8 指標)
    max_drawdown_tick_steps: int
    total_pnl_tick_steps: int


def build_report(trades: list[TradeRecord]) -> BacktestReport:
    equity: list[int] = []
    total = 0
    for t in trades:
        total += t.pnl_tick_steps
        equity.append(total)

    gross_win = sum(t.pnl_tick_steps for t in trades if t.pnl_tick_steps > 0)
    gross_loss = -sum(t.pnl_tick_steps for t in trades if t.pnl_tick_steps < 0)
    pf = (Decimal(gross_win) / gross_loss).quantize(Q) if gross_loss > 0 else None

    n = len(trades)
    wins = sum(1 for t in trades if t.pnl_tick_steps > 0)
    be_exits = sum(1 for t in trades if t.is_breakeven_sl_exit)

    peak = 0
    max_dd = 0
    for v in equity:
        peak = max(peak, v)
        max_dd = max(max_dd, peak - v)

    return BacktestReport(
        trades=tuple(trades),
        equity_curve=tuple(equity),
        profit_factor=pf,
        win_rate=(Decimal(wins) / n).quantize(Q) if n else Decimal(0),
        be_sl_exit_rate=(Decimal(be_exits) / n).quantize(Q) if n else Decimal(0),
        max_drawdown_tick_steps=max_dd,
        total_pnl_tick_steps=total,
    )


# ---------------------------------------------------------------------------
# トレード詳細レコーダー (レポート可視化用フック)
# ---------------------------------------------------------------------------

class TradeRecorder(Protocol):
    """run(recorder=...) に渡すと、約定済みトレードの詳細 (台帳・FSMジャーナル・
    プラン) と未約定キャンセルを受け取れる。実装は backtest/report_html.py。"""

    def on_trade_closed(self, record: TradeRecord, ledger: _Ledger,
                        fsm, plan: TradePlan) -> None: ...
    def on_unfilled(self, position_id: str, fsm, plan: TradePlan) -> None: ...


# ---------------------------------------------------------------------------
# エンジン本体
# ---------------------------------------------------------------------------

class BacktestEquityProvider:
    """バックテスト中の口座残高を追跡する EquityProvider 実装。

    initial_capital_usd + 累積実現損益 (tick_steps × tick_value_usd_per_step) を返す。
    _finalize() からトレードが確定するたびに record_pnl() で更新する。
    """

    def __init__(self, initial_capital_usd: Decimal,
                 tick_value_usd_per_step: Decimal) -> None:
        self._initial = initial_capital_usd
        self._tick_value = tick_value_usd_per_step
        self._cumulative: int = 0  # tick_steps 単位の累積損益

    def record_pnl(self, pnl_tick_steps: int) -> None:
        self._cumulative += pnl_tick_steps

    def equity(self) -> Decimal:
        return self._initial + Decimal(self._cumulative) * self._tick_value


class BacktestEngine:
    def __init__(self, *, broker: LedgerBroker, gateway: AiGateway,
                 risk: RiskManager, fsm_config: FsmConfig,
                 swap: SwapConfig | None = None,
                 volume_sizer: "VolumeSizer | None" = None,
                 equity_provider: "BacktestEquityProvider | None" = None) -> None:
        self._broker = broker
        self._gateway = gateway
        self._risk = risk
        self._fsm_cfg = fsm_config
        self._swap = swap or SwapConfig()
        self._sizer = volume_sizer
        self._equity_prov = equity_provider

    # -- Pass 1: 裁定イベント収集 (LLMは一切呼ばない) ------------------------------

    @staticmethod
    def collect_judgements(
        candles: Iterable[Candle],
        provider: SignalProvider,
        *,
        policy: EscalationPolicy,
        cache: VerdictCache,
        tier: str = "ALL",
    ) -> list[tuple[JudgementRequest, str]]:
        """エスカレーション対象 (キャッシュ未解決) の (request, tier) を重複なく収集。

        戻り値を ai.batch.write_batch_file へ渡せば Batch API 入力が完成する。

        tier による2段階収集 (L2=Fable 5 のコスト制御):
          - "L1" : L1 (Haiku) 分のみ収集する (第1弾)
          - "L2" : L1 の verdict が cache 上で GO のプランに限り L2 分を収集する
                   (第2弾。L1未解決/却下のプランはゲートウェイ仕様上 L2 に
                   到達しないため課金しない)。日次 L2 予算
                   (policy.l2_daily_call_cap, UTC確定足基準) もライブ同様に適用
          - "ALL": L1/L2 を一括収集 (従来挙動。L1 の結果を待たないため
                   L2 課金が最大になる)
        """
        if tier not in ("ALL", "L1", "L2"):
            raise ValueError(f"tier must be ALL/L1/L2, got {tier!r}")

        pending: dict[str, tuple[JudgementRequest, str]] = {}
        current_day = None
        l2_used_today = 0
        for candle in candles:
            if tier == "L2":
                bar_day = candle.open_time.date()
                if bar_day != current_day:
                    current_day = bar_day
                    l2_used_today = 0
            for plan in provider.on_candle(candle).plans:
                decided = policy.decide(plan.cluster_score, plan.ambiguity)
                if decided is Tier.NONE:
                    continue

                if tier in ("ALL", "L1"):
                    key = cache_key(plan.request, "L1")
                    if key not in pending and cache.get(key) is None:
                        pending[key] = (plan.request, "L1")

                if decided is not Tier.L2_AFTER_L1:
                    continue
                if tier == "ALL":
                    key = cache_key(plan.request, "L2")
                    if key not in pending and cache.get(key) is None:
                        pending[key] = (plan.request, "L2")
                elif tier == "L2":
                    l2_key = cache_key(plan.request, "L2")
                    if l2_key in pending or cache.get(l2_key) is not None:
                        continue
                    l1 = cache.get(cache_key(plan.request, "L1"))
                    if l1 is None or l1.decision != "GO":
                        continue          # L1未解決/却下 → L2は呼ばれない (gateway仕様)
                    if l2_used_today >= policy.l2_daily_call_cap:
                        continue          # 日次予算 (ライブと同じ制約で収集)
                    pending[l2_key] = (plan.request, "L2")
                    l2_used_today += 1
        return list(pending.values())

    # -- Pass 2: 再現執行 (TradingLoop = ライブと同一コードパス) --------------------

    def run(self, candles: Iterable[Candle], provider: SignalProvider, *,
            recorder: "TradeRecorder | None" = None) -> BacktestReport:
        # 失効リカバリー: 打診の時間切れ失効を provider へ通知しクールダウンを
        # 即時解除させる (provider 側で opt-in 判定。未実装の provider は no-op)。
        expiry_sink = getattr(provider, "notify_probe_expired", None)
        loop = TradingLoop(broker=self._broker, gateway=self._gateway,
                           risk=self._risk, fsm_config=self._fsm_cfg,
                           expiry_sink=expiry_sink,
                           volume_sizer=self._sizer,
                           equity_provider=self._equity_prov)
        trades: list[TradeRecord] = []

        for candle in candles:
            loop.on_broker_events(self._broker.process_bar(candle))
            output = provider.on_candle(candle)
            # クローズ前にスナップショット (FSM/プランは on_candle 内で削除されるため)
            snapshot = dict(loop.open_positions) if recorder is not None else {}
            for pid in loop.on_candle(candle, output, spread_ticks=self._broker.spread):
                self._finalize(pid, trades, recorder, snapshot.get(pid))

        snapshot = dict(loop.open_positions) if recorder is not None else {}
        for pid in loop.close_all_open("END_OF_DATA"):
            self._finalize(pid, trades, recorder, snapshot.get(pid))

        return build_report(trades)

    def _finalize(self, position_id: str, trades: list[TradeRecord],
                  recorder: "TradeRecorder | None" = None,
                  fsm_plan: tuple | None = None) -> None:
        led = self._broker.ledgers.get(position_id)
        if led is None or not led.entries or not led.exits:
            if recorder is not None and led is not None and fsm_plan is not None:
                recorder.on_unfilled(position_id, fsm_plan[0], fsm_plan[1])
            self._broker.ledgers.pop(position_id, None)
            return  # 未約定キャンセルはトレードとして数えない
        entry_value = sum(p * v for p, v in led.entries)
        exit_value = sum(p * v for p, v in led.exits)
        gross = led.direction * (exit_value - entry_value)
        swap = compute_swap_tick_steps(led.direction, led, self._swap)
        record = TradeRecord(
            position_id=position_id, direction=led.direction,
            pnl_tick_steps=gross + swap, exit_kind=led.exit_kind,
            swap_tick_steps=swap)
        trades.append(record)
        if recorder is not None and fsm_plan is not None:
            recorder.on_trade_closed(record, led, fsm_plan[0], fsm_plan[1])
        self._risk.record_realized(record.pnl_tick_steps)
        if self._equity_prov is not None:
            self._equity_prov.record_pnl(record.pnl_tick_steps)
        del self._broker.ledgers[position_id]


# ---------------------------------------------------------------------------
# ヒストリカルデータ読み込み (Parquet, float列の旧形式)
# ---------------------------------------------------------------------------

def load_candles_parquet(path: str | Path, spec: SymbolSpec, tf: Timeframe) -> list[Candle]:
    """Parquet (列: time[UTC], open, high, low, close, volume) から確定足を読む。

    float価格列の外部データ用。INFERS自身が書き出す整数ティック形式は
    data/exporter.py の load_history を使うこと。
    """
    try:
        import polars as pl  # 遅延 import
    except ImportError as e:
        raise RuntimeError("polars required: pip install polars") from e

    df = pl.read_parquet(str(path)).sort("time")
    candles: list[Candle] = []
    for row in df.iter_rows(named=True):
        candles.append(Candle(
            symbol=spec.name, tf=tf, open_time=row["time"],
            o_int=spec.float_to_ticks(row["open"]),
            h_int=spec.float_to_ticks(row["high"]),
            l_int=spec.float_to_ticks(row["low"]),
            c_int=spec.float_to_ticks(row["close"]),
            volume=int(row["volume"]), is_closed=True,
        ))
    return candles

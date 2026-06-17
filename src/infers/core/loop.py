"""共通取引ループ (設計書 §1.1 / CLAUDE.md 第12条: ライブ・バックテスト同等性)。

BacktestEngine (backtest/engine.py) と LiveRunner (execution/mt5_adapter.py) は
どちらも本 TradingLoop を駆動する。差し替わるのはフィードとブローカーのみで、
「確定足1本に対する判断と執行」のコードパスは完全に同一である。

ループの責務 (毎確定足):
  1. ブローカーイベント (約定/SLヒット) を FSM へ配送
  2. 既存ポジションの管理 — すべて決定論 (LLM非依存の防御: 第1条)
  3. 新規プラン → AIゲート → リスク拒否権 → 打診発注
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Callable, Iterable, Protocol

from infers.ai.gateway import AiGateway, JudgementRequest
from infers.analysis.dow import StructureEvent
from infers.analysis.support_resistance import SRZone
from infers.data.models import Candle
from infers.execution.risk import OrderRequest, RiskManager
from infers.execution.sim_broker import BrokerEvent
from infers.execution.sm import BrokerPort, FsmConfig, PositionFSM, PosState


@dataclass(frozen=True)
class TradePlan:
    """L0が組み上げた打診プラン (AIゲート審査前)。価格はすべて整数ティック。"""

    plan_id: str
    direction: int
    limit_price_int: int
    volume_steps: int
    add_volume_steps: int
    sl_int: int
    expiry: datetime
    invalidation_price: int
    w1_high_int: int                  # 追撃トリガーの基準 (設計書 §6.2)
    fib_target_int: int               # 半分利確の161.8%目標 (設計書 §6.4)
    request: JudgementRequest
    cluster_score: Decimal
    ambiguity: Decimal
    # エントリー根拠の可視化用 (省略可。ライブ執行には不要)。
    w1_low_int: int = 0                          # 第1波起点 (FIB押し戻しの基準)
    fib_levels: tuple[int, ...] = ()             # 第1波の押し戻し水準価格 (38.2/50/61.8/78.6)
    sr_zones: tuple[tuple[int, int], ...] = ()   # エントリー近傍の重要SRゾーン (low,high)


@dataclass
class ProviderOutput:
    plans: list[TradePlan] = field(default_factory=list)
    structure_events: list[StructureEvent] = field(default_factory=list)
    rsi_value: Decimal | None = None              # 現在RSI (半分利確トリガー: §6.4)
    tp_sr_zones: tuple[SRZone, ...] = ()          # 重要SRゾーン (半分利確トリガー: §6.4)
    sma90_int: int | None = None                  # 90SMA値 (半分利確トリガー: ③-1)


class SignalProvider(Protocol):
    """確定足を受けてプランと構造イベントを発行する (分析層の集約点)。"""

    def on_candle(self, candle: Candle) -> ProviderOutput: ...

    def notify_probe_expired(self, position_id: str | None = None) -> None:
        """打診指値の時間切れ失効を戦略層へ通知 (失効リカバリー)。任意実装。"""
        ...


class JournalSink(Protocol):
    """追記専用ジャーナルへの記録口 (CLAUDE.md 第11条)。

    具象は infers.journal.JournalWriter。loop はこの構造的プロトコルにのみ
    依存し、ファイルI/Oの詳細・import を持たない (戦略コアの純粋性: 第12条)。
    """

    def set_bar(self, bar_time: datetime) -> None: ...
    def record(self, kind: str, data: dict) -> None: ...
    def fsm_sink(self, position_id: str) -> Callable[[str, dict], None]: ...


class TradingLoop:
    """ポジション群のオーケストレーション (モード非依存の中核)。"""

    def __init__(self, *, broker: BrokerPort, gateway: AiGateway,
                 risk: RiskManager, fsm_config: FsmConfig,
                 journal: "JournalSink | None" = None,
                 expiry_sink: "Callable[[str], None] | None" = None) -> None:
        self._broker = broker
        self._gateway = gateway
        self._risk = risk
        self._fsm_cfg = fsm_config
        # 追記専用ジャーナル (ライブのみ注入。None でバックテスト同等の純粋経路)。
        self._journal = journal
        # 失効リカバリー: 打診指値が「時間切れ (expired)」でキャンセルされた瞬間に
        # 戦略プロバイダへ通知し、当該系列のクールダウンを即時解除させるフック。
        # 無効化 (invalidated = シナリオ崩壊) では呼ばない (entry-methodology.md ※例外)。
        # None で従来挙動 (リカバリーなし)。プロバイダ側が opt-in を判定するため、
        # 配線は常時行ってよい (フラグ無効時は no-op)。
        self._expiry_sink = expiry_sink
        self.open_positions: dict[str, tuple[PositionFSM, TradePlan]] = {}
        self._current_day: date | None = None

    # -- 1) ブローカーイベント配送 -------------------------------------------------

    def on_broker_events(self, events: Iterable[BrokerEvent]) -> None:
        for ev in events:
            entry = self.open_positions.get(ev.position_id)
            if entry is None:
                continue
            fsm, _ = entry
            if ev.kind == "FILL" and fsm.state is PosState.PROBE_PENDING:
                fsm.on_probe_fill(ev.price_int, ev.volume_steps)
            elif ev.kind == "SL_HIT":
                fsm.on_sl_hit(ev.price_int)

    # -- 2)+3) 確定足処理 ----------------------------------------------------------

    def on_candle(self, candle: Candle, output: ProviderOutput, *,
                  spread_ticks: int) -> list[str]:
        """確定足1本を処理し、この足でクローズしたポジションIDを返す。"""
        closed: list[str] = []

        # ジャーナルの決定論アンカー (以降の全イベントをこの確定足に紐づける)
        if self._journal is not None:
            self._journal.set_bar(candle.close_time)

        # 日次境界 (UTC確定足基準で決定論): 日次損失カウンタ・L2予算をリセット。
        # キルスイッチのラッチは new_day では解除されない (リスク層の契約)。
        bar_day = candle.open_time.date()
        if self._current_day is None:
            self._current_day = bar_day
        elif bar_day != self._current_day:
            self._current_day = bar_day
            self._risk.new_day()
            self._gateway.new_day()

        # 既存ポジションの管理 (決定論。AI/リスク層を一切経由しない)
        for pid, (fsm, plan) in list(self.open_positions.items()):
            if fsm.state is PosState.PROBE_PENDING:
                reason = fsm.on_bar_pending(candle)
                if reason == "expired" and self._expiry_sink is not None:
                    # 時間切れ失効 → クールダウン即時解除 (機会損失のリカバリー)。
                    # 無効化 (シナリオ崩壊) では解除しない。
                    self._expiry_sink(pid)
            for sev in output.structure_events:
                fsm.on_structure_event(sev)
            if fsm.state is PosState.PROBE:
                fsm.on_wave1_break(candle, plan.w1_high_int,
                                   add_volume_steps=plan.add_volume_steps)
            if fsm.state is PosState.SL_AT_BE:
                # 半分利確: RSI利確圏 / 90SMA接触 / 重要SRゾーン到達 (③-1)
                fsm.on_half_tp_signal(candle, output.rsi_value, output.tp_sr_zones,
                                      output.sma90_int)
            elif fsm.state is PosState.RUNNER:
                # 残玉決済 (entry-methodology.md ③-2): ランナーはフィボ目標まで伸ばし、
                # 下方は建値SLで保護する。ダウ転換クローズは手法に無いため既定で行わない
                # (runner_reversal_exit=True で旧挙動)。
                filled = fsm.on_runner_target(candle, plan.fib_target_int)
                if not filled and self._fsm_cfg.runner_reversal_exit:
                    for sev in output.structure_events:
                        if fsm.state is PosState.RUNNER and fsm.on_runner_reversal(sev):
                            break
            if fsm.state is PosState.CLOSED:
                closed.append(pid)
                del self.open_positions[pid]

        # 新規プラン → AIゲート → リスク拒否権 → 打診発注
        open_volume = sum(f.volume_steps for f, _ in self.open_positions.values())
        for plan in output.plans:
            if plan.plan_id in self.open_positions:
                continue  # 冪等: 同一プランの再発行は無視 (二重建て防止)
            verdict = self._gateway.judge(
                plan.request,
                cluster_score=plan.cluster_score, ambiguity=plan.ambiguity)
            if self._journal is not None:
                # 判断を特徴量スナップショットとともに記録 (CLAUDE.md 第11条)。
                # 「ログに出してない判断」を作らない — GO/NO_GO いずれも残す。
                self._journal.record("VERDICT", {
                    "plan_id": plan.plan_id,
                    "direction": plan.direction,
                    "symbol": plan.request.symbol,
                    "kind": plan.request.kind.value,
                    "decision": verdict.decision,
                    "source": verdict.source,
                    "confidence": str(verdict.confidence),
                    "reasons": list(verdict.reasons),
                    "cluster_score": str(plan.cluster_score),
                    "ambiguity": str(plan.ambiguity),
                    "features": plan.request.features,
                })
            if verdict.decision != "GO":
                continue
            ok = self._risk.approve(
                OrderRequest(symbol=plan.request.symbol, direction=plan.direction,
                             volume_steps=plan.volume_steps, kind="PROBE_LIMIT"),
                current_spread_ticks=spread_ticks,
                open_total_volume_steps=open_volume,
            )
            if not ok:
                if self._journal is not None:
                    self._journal.record("RISK_REJECT", {
                        "plan_id": plan.plan_id, "direction": plan.direction,
                        "volume_steps": plan.volume_steps,
                        "spread_ticks": spread_ticks})
                continue
            fsm = PositionFSM(position_id=plan.plan_id, direction=plan.direction,
                              broker=self._broker, config=self._fsm_cfg,
                              journal_sink=(self._journal.fsm_sink(plan.plan_id)
                                            if self._journal is not None else None))
            fsm.place_probe(
                limit_price_int=plan.limit_price_int,
                volume_steps=plan.volume_steps, sl_int=plan.sl_int,
                expiry=plan.expiry, invalidation_price=plan.invalidation_price)
            self.open_positions[plan.plan_id] = (fsm, plan)
            open_volume += plan.volume_steps

        return closed

    # -- 終了処理 -------------------------------------------------------------------

    def close_all_open(self, reason: str) -> list[str]:
        """残存ポジションの手仕舞い (データ末尾・シャットダウン)。"""
        closed: list[str] = []
        for pid, (fsm, _) in list(self.open_positions.items()):
            if fsm.state is PosState.PROBE_PENDING:
                fsm.abort_pending(reason)
            elif fsm.state is not PosState.CLOSED:
                fsm.close_all(reason)
            closed.append(pid)
            del self.open_positions[pid]
        return closed

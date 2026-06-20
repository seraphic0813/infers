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

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Callable, Iterable, Protocol

from infers.ai.gateway import AiGateway, JudgementRequest
from infers.analysis.dow import StructureEvent
from infers.analysis.support_resistance import SRZone
from infers.core.execution import ExecutionModel
from infers.core.models import Candle
from infers.execution.risk import OrderRequest, RiskManager, VolumeSizer
from infers.execution.sim_broker import BrokerEvent
from infers.execution.sm import BrokerPort, FsmConfig


class EquityProvider(Protocol):
    """現在の口座残高 (equity) をアカウント通貨建てで返す。

    バックテストは累積損益から算出し、ライブは MT5 account_info().equity を返す。
    VolumeSizer と同一通貨建てで提供することが契約。
    """

    def equity(self) -> Decimal: ...


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
                 expiry_sink: "Callable[[str], None] | None" = None,
                 volume_sizer: "VolumeSizer | None" = None,
                 equity_provider: "EquityProvider | None" = None,
                 execution_factory: "Callable[..., ExecutionModel] | None" = None) -> None:
        self._broker = broker
        self._gateway = gateway
        self._risk = risk
        self._fsm_cfg = fsm_config
        # 執行モデルの生成器 (段階2.3)。手法ごとに執行ライフサイクルを差し替える
        # 注入点。None のときは既定の Narrow Focus 執行 (NarrowFocusExecution) を
        # 遅延 import で構築する (L0 がモジュールレベルで L2 を import しないため)。
        self._execution_factory = execution_factory
        # 追記専用ジャーナル (ライブのみ注入。None でバックテスト同等の純粋経路)。
        self._journal = journal
        # 失効リカバリー: 打診指値が「時間切れ (expired)」でキャンセルされた瞬間に
        # 戦略プロバイダへ通知し、当該系列のクールダウンを即時解除させるフック。
        # 無効化 (invalidated = シナリオ崩壊) では呼ばない (entry-methodology.md ※例外)。
        # None で従来挙動 (リカバリーなし)。プロバイダ側が opt-in を判定するため、
        # 配線は常時行ってよい (フラグ無効時は no-op)。
        self._expiry_sink = expiry_sink
        # 可変ロットサイジング: 両方 None なら plan.volume_steps の固定値を使う (旧挙動)。
        self._sizer = volume_sizer
        self._equity_prov = equity_provider
        self.open_positions: dict[str, tuple[ExecutionModel, TradePlan]] = {}
        self._current_day: date | None = None

    def _new_execution(self, position_id: str, direction: int) -> ExecutionModel:
        """執行モデルを1つ生成する (抽象 ExecutionModel を返す)。"""
        journal_sink = (self._journal.fsm_sink(position_id)
                        if self._journal is not None else None)
        if self._execution_factory is not None:
            return self._execution_factory(
                position_id=position_id, direction=direction,
                broker=self._broker, config=self._fsm_cfg, journal_sink=journal_sink)
        # 既定: Narrow Focus 執行。遅延 import で L0→L2 のモジュール依存を避ける。
        from infers.execution.sm import NarrowFocusExecution
        return NarrowFocusExecution(
            position_id=position_id, direction=direction,
            broker=self._broker, config=self._fsm_cfg, journal_sink=journal_sink)

    # -- 1) ブローカーイベント配送 -------------------------------------------------

    def on_broker_events(self, events: Iterable[BrokerEvent]) -> None:
        for ev in events:
            entry = self.open_positions.get(ev.position_id)
            if entry is None:
                continue
            execution, _ = entry
            execution.on_broker_event(ev)

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

        # 既存ポジションの管理 (決定論。AI/リスク層を一切経由しない)。執行モデル
        # 抽象 (ExecutionModel.on_bar) に委譲し、ループは手法固有の執行手順を知らない。
        for pid, (execution, _plan) in list(self.open_positions.items()):
            outcome = execution.on_bar(candle, output)
            if outcome.expired and self._expiry_sink is not None:
                # 時間切れ失効 → クールダウン即時解除 (機会損失のリカバリー)。
                # 無効化 (シナリオ崩壊) では解除しない。
                self._expiry_sink(pid)
            if outcome.closed:
                closed.append(pid)
                del self.open_positions[pid]

        # 新規プラン → AIゲート → リスク拒否権 → 打診発注
        open_volume = sum(em.volume_steps for em, _ in self.open_positions.values())
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
            # 可変ロットサイジング: EquityProvider + VolumeSizer が注入されている場合は
            # 残高 × risk_pct ÷ SL距離 で発注ステップ数を決定する。
            # 注入なし (省略時) は plan の固定値をそのまま使う (旧挙動・テスト互換)。
            sl_dist = abs(plan.limit_price_int - plan.sl_int)
            if self._sizer is not None and self._equity_prov is not None:
                sized_volume = self._sizer.calc_volume_steps(
                    self._equity_prov.equity(), sl_dist)
                sized_add = sized_volume  # ADD は PROBE と同量
                sized_plan = dataclasses.replace(
                    plan, volume_steps=sized_volume, add_volume_steps=sized_add)
            else:
                sized_plan = plan
            ok = self._risk.approve(
                OrderRequest(symbol=plan.request.symbol, direction=plan.direction,
                             volume_steps=sized_plan.volume_steps, kind="PROBE_LIMIT"),
                current_spread_ticks=spread_ticks,
                open_total_volume_steps=open_volume,
            )
            if not ok:
                if self._journal is not None:
                    self._journal.record("RISK_REJECT", {
                        "plan_id": plan.plan_id, "direction": plan.direction,
                        "volume_steps": sized_plan.volume_steps,
                        "spread_ticks": spread_ticks})
                continue
            execution = self._new_execution(plan.plan_id, plan.direction)
            execution.place(sized_plan)
            self.open_positions[plan.plan_id] = (execution, sized_plan)
            open_volume += sized_plan.volume_steps

        return closed

    # -- 終了処理 -------------------------------------------------------------------

    def close_all_open(self, reason: str) -> list[str]:
        """残存ポジションの手仕舞い (データ末尾・シャットダウン)。"""
        closed: list[str] = []
        for pid, (execution, _) in list(self.open_positions.items()):
            execution.close(reason)
            closed.append(pid)
            del self.open_positions[pid]
        return closed

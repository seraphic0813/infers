"""market_tpsl 執行モデル: 成行参入 + 固定TP/SL (L2 / 段階2.5)。

core.execution.ExecutionModel を構造的に充足する2つ目の執行モデル。Narrow Focus の
PROBE_PENDING→PROBE→ADD→SL_AT_BE→RUNNER という多段ライフサイクルとは異なり、
IDLE→OPEN→CLOSED の最小ライフサイクルだけを持つ:

  - place(): 即時に成行で参入し、固定SLを発注時に設定する (SLなしの状態は作らない)。
  - on_bar(): 確定足の高値/安値が固定TPへ到達したら全決済 (確定足主義)。
  - on_broker_event(): ブローカーのSLヒットで CLOSED へ。
  - close(): 残存ポジションを成行手仕舞い (データ末尾・シャットダウン)。

安全原則 (CLAUDE.md §0) は本モデルにも等しく強制される:
  - LLM非依存 — 本モジュールは LLM を一切呼ばない (防御は決定論)。
  - 確定足主義 — on_bar は確定足のみ受領する。
  - SL単調性 — 固定SLは一度も動かさない (move_sl 経路を持たない) ため自明に満たす。
  - 冪等性 — 全注文に決定論的 client_order_id を付与する。
"""

from __future__ import annotations

from enum import Enum, auto
from typing import Callable

from infers.core.execution import BarOutcome, BrokerPort
from infers.core.models import Candle


class MarketState(Enum):
    IDLE = auto()      # 未参入
    OPEN = auto()      # 成行約定済み (TP/SL待ち)
    CLOSED = auto()    # 終了 (TP到達・SLヒット・強制手仕舞い)


class MarketTpSlExecution:
    """成行参入 + 固定TP/SL の執行モデル (ExecutionModel を構造的に充足)。"""

    def __init__(self, *, position_id: str, direction: int,
                 broker: BrokerPort, config: object = None,
                 journal_sink: Callable[[str, dict], None] | None = None) -> None:
        if direction not in (+1, -1):
            raise ValueError("direction must be +1 or -1")
        self.position_id = position_id
        self.direction = direction
        self._broker = broker
        # config は受け取るが本モデルでは未使用 (TP/SL/数量は intent から取る)。
        # TradingLoop の生成器シグネチャ互換のために受ける。
        self._journal_sink = journal_sink

        self._state = MarketState.IDLE
        self._seq = 0
        self._sl_int: int | None = None
        self._tp_int: int | None = None
        self._entry_int: int | None = None
        self._volume_steps = 0
        self.journal: list[tuple[str, dict]] = []
        # 可視化フック互換 (recorder は execution の plan を参照しうる)。
        self.plan: object | None = None

    # -- 参照 (ExecutionModel 抽象) ------------------------------------------------

    @property
    def state(self) -> MarketState:
        return self._state

    @property
    def volume_steps(self) -> int:
        return self._volume_steps

    @property
    def closed(self) -> bool:
        return self._state is MarketState.CLOSED

    @property
    def sl_int(self) -> int | None:
        return self._sl_int

    @property
    def entry_price_int(self) -> int | None:
        return self._entry_int

    # -- 内部ヘルパー --------------------------------------------------------------

    def _oid(self, tag: str) -> str:
        """決定論的 client_order_id (冪等性: CLAUDE.md 第10条)。"""
        self._seq += 1
        return f"{self.position_id}/{self._seq:03d}/{tag}"

    def _log(self, transition: str, **payload: object) -> None:
        entry = dict(payload, state=self._state.name)
        self.journal.append((transition, entry))
        if self._journal_sink is not None:
            self._journal_sink(transition, entry)

    def _profit_side(self, a: int, b: int) -> int:
        """direction 正規化差分: 利益方向に正。"""
        return self.direction * (a - b)

    def _require_closed_bar(self, candle: Candle) -> None:
        if not candle.is_closed:
            raise ValueError("decisions only on closed bars (CLAUDE.md rule 2)")

    # -- ExecutionModel 抽象 -------------------------------------------------------

    def place(self, intent: object) -> None:
        """成行参入。SLは発注と同時に必ず設定する。

        intent は duck-typed (limit_price_int=参入参考価格 / sl_int / volume_steps /
        fib_target_int=固定TP)。TradePlan をそのまま受け取れる。
        """
        if self._state is not MarketState.IDLE:
            raise RuntimeError("place() only allowed in IDLE")
        if intent.volume_steps < 1:
            raise ValueError("market volume must be >= 1 step")
        if self._profit_side(intent.limit_price_int, intent.sl_int) <= 0:
            raise ValueError("sl must be on the losing side of entry")
        if self._profit_side(intent.fib_target_int, intent.limit_price_int) <= 0:
            raise ValueError("tp must be on the winning side of entry")

        self._sl_int = intent.sl_int
        self._tp_int = intent.fib_target_int
        self.plan = intent
        fill = self._broker.place_market(
            client_order_id=self._oid("mkt_entry"), position_id=self.position_id,
            direction=self.direction, volume_steps=intent.volume_steps, sl_int=intent.sl_int)
        self._entry_int = fill
        self._volume_steps = intent.volume_steps
        self._state = MarketState.OPEN
        self._log("MARKET_ENTRY", fill=fill, sl=self._sl_int, tp=self._tp_int,
                  volume=intent.volume_steps)

    def on_broker_event(self, ev: object) -> None:
        """ブローカーのSLヒットで CLOSED へ (成行参入は別途FILLイベントを伴わない)。"""
        if ev.kind == "SL_HIT" and self._state is MarketState.OPEN:
            self._volume_steps = 0
            self._state = MarketState.CLOSED
            self._log("SL_HIT", price=ev.price_int)

    def on_bar(self, candle: Candle, signal: object) -> BarOutcome:
        """確定足の高値(買い)/安値(売り)が固定TPへ到達したら全決済する。"""
        if self._state is MarketState.OPEN and self._tp_int is not None:
            self._require_closed_bar(candle)
            touch = candle.h_int if self.direction > 0 else candle.l_int
            if self._profit_side(touch, self._tp_int) >= 0:
                self._broker.close_volume(
                    client_order_id=self._oid("tp_close"),
                    position_id=self.position_id, volume_steps=self._volume_steps)
                self._volume_steps = 0
                self._state = MarketState.CLOSED
                self._log("TP_CLOSE", tp=self._tp_int)
        return BarOutcome(closed=self._state is MarketState.CLOSED, expired=False)

    def close(self, reason: str) -> None:
        """残存ポジションの成行手仕舞い (データ末尾・シャットダウン)。"""
        if self._state is MarketState.OPEN:
            self._broker.close_volume(
                client_order_id=self._oid("close_all"),
                position_id=self.position_id, volume_steps=self._volume_steps)
            self._volume_steps = 0
            self._state = MarketState.CLOSED
            self._log("CLOSE_ALL", reason=reason)

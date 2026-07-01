"""atr_trend_scalp 執行モデル: 成行参入 + 50/50分割 + 建値化 + トレール + 段階TP
(L2 / spec.md §3・§4)。

`core.execution.ExecutionModel` を構造的に充足する執行モデル:

  - place(): 成行で全玉参入し、初期SL(= entry ∓ 1.0×ATR)を発注時に設定する。
  - on_bar(): 確定足ごとに
      OPEN   → 高値(買)/安値(売)が TP1 到達 → 半玉を利確 + 残玉SLを建値へ前進 → RUNNER
      RUNNER → 0.5×ATR トレール(利益方向のみ)+ TP2 到達で残玉全決済 → CLOSED
  - on_broker_event(): ブローカーのSLヒットで CLOSED(初期SL/建値/トレールいずれでも)。
  - close(): 残玉を成行手仕舞い(データ末尾・シャットダウン)。

**A-4(防御トリガー種別は手法契約)**: 本手法は含み益トリガー(TP1到達で建値化、
その後 0.5×ATR トレール)を **自手法の契約として採用**する。判定は PnL額ではなく
確定足の価格水準到達で行う(A-2 確定足主義に準拠。実装上は価格比較)。CLAUDE.md の
A-4 手法スコープ化に基づき許可される(Narrow Focus はこの自由を使わない)。

安全原則(CLAUDE.md §A)は本モデルにも等しく強制される:
  - A-1 LLM非依存 — 本モジュールは LLM を一切呼ばない(防御は決定論)。
  - A-2 確定足主義 — on_bar は確定足のみ受領する。
  - A-3 SL単調性 — `_advance_sl_to` が利益方向への改善のみを許可(no-op方式)。
  - A-9 冪等性 — 全注文に決定論的 client_order_id を付与する。

半玉数(`half`)はプランに焼き込まず place() 時点の実 volume_steps から導出する
(可変ロット `--risk-pct` で volume がリサイズされ得るため)。volume が1で半玉が
作れない場合は TP1 での部分利確を行わず、建値化とトレールのみ適用する(残玉が全量)。
"""

from __future__ import annotations

from enum import Enum, auto
from typing import Callable

from infers.core.execution import BarOutcome, BrokerPort
from infers.core.models import Candle


class AtrState(Enum):
    IDLE = auto()      # 未参入
    OPEN = auto()      # 全玉約定済み(TP1/初期SL待ち)
    RUNNER = auto()    # TP1後の残玉(建値SL+トレール、TP2待ち)
    CLOSED = auto()    # 終了


class AtrTrendExecution:
    """成行参入 + 分割決済(TP1半利+建値化+トレール+TP2)の執行モデル。"""

    def __init__(self, *, position_id: str, direction: int,
                 broker: BrokerPort, config: object = None,
                 journal_sink: Callable[[str, dict], None] | None = None) -> None:
        if direction not in (+1, -1):
            raise ValueError("direction must be +1 or -1")
        self.position_id = position_id
        self.direction = direction
        self._broker = broker
        self._journal_sink = journal_sink

        self._state = AtrState.IDLE
        self._seq = 0
        self._sl_int: int | None = None
        self._tp1_int: int | None = None
        self._tp2_int: int | None = None
        self._trail_distance: int | None = None
        self._entry_int: int | None = None
        self._volume_steps = 0
        self._half_steps = 0
        self.journal: list[tuple[str, dict]] = []
        self.plan: object | None = None

    # -- 参照 (ExecutionModel 抽象) ------------------------------------------------

    @property
    def state(self) -> AtrState:
        return self._state

    @property
    def volume_steps(self) -> int:
        return self._volume_steps

    @property
    def closed(self) -> bool:
        return self._state is AtrState.CLOSED

    @property
    def sl_int(self) -> int | None:
        return self._sl_int

    @property
    def entry_price_int(self) -> int | None:
        return self._entry_int

    # -- 内部ヘルパー --------------------------------------------------------------

    def _oid(self, tag: str) -> str:
        """決定論的 client_order_id (冪等性: CLAUDE.md 第9条)。"""
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

    def _advance_sl_to(self, target: int) -> None:
        """SLを target へ前進する。利益方向への改善でなければ no-op(A-3 SL単調性)。

        建値化・トレールとも同一トリガーが複数バーに渡って継続し得る(冪等な再前進が
        前提)ため、改善が無ければ例外ではなく静かに無視する(smc_bos と同方式)。
        """
        assert self._sl_int is not None
        if self._profit_side(target, self._sl_int) <= 0:
            return
        self._broker.modify_sl(position_id=self.position_id, new_sl_int=target)
        self._sl_int = target
        self._log("MOVE_SL", new_sl=target)

    # -- ExecutionModel 抽象 -------------------------------------------------------

    def place(self, intent: object) -> None:
        """成行で全玉参入。初期SLを発注と同時に必ず設定する。

        intent は duck-typed (limit_price_int / sl_int / tp1_int / fib_target_int(=TP2) /
        trail_distance_ticks / volume_steps)。`AtrTrendPlan` をそのまま受け取れる。
        """
        if self._state is not AtrState.IDLE:
            raise RuntimeError("place() only allowed in IDLE")
        if intent.volume_steps < 1:
            raise ValueError("market volume must be >= 1 step")
        if self._profit_side(intent.limit_price_int, intent.sl_int) <= 0:
            raise ValueError("sl must be on the losing side of entry")
        if self._profit_side(intent.tp1_int, intent.limit_price_int) <= 0:
            raise ValueError("tp1 must be on the winning side of entry")
        if self._profit_side(intent.fib_target_int, intent.tp1_int) <= 0:
            raise ValueError("tp2 must be beyond tp1")
        if intent.trail_distance_ticks < 1:
            raise ValueError("trail_distance_ticks must be >= 1")

        self._sl_int = intent.sl_int
        self._tp1_int = intent.tp1_int
        self._tp2_int = intent.fib_target_int
        self._trail_distance = intent.trail_distance_ticks
        self.plan = intent
        fill = self._broker.place_market(
            client_order_id=self._oid("mkt_entry"), position_id=self.position_id,
            direction=self.direction, volume_steps=intent.volume_steps, sl_int=intent.sl_int)
        self._entry_int = fill
        self._volume_steps = intent.volume_steps
        self._half_steps = intent.volume_steps // 2   # 端数は残玉(RUNNER)側へ寄せる
        self._state = AtrState.OPEN
        self._log("MARKET_ENTRY", fill=fill, sl=self._sl_int, tp1=self._tp1_int,
                  tp2=self._tp2_int, trail=self._trail_distance,
                  volume=intent.volume_steps, half=self._half_steps)

    def on_broker_event(self, ev: object) -> None:
        """ブローカーのSLヒットで CLOSED へ(初期SL/建値/トレールいずれの水準でも)。"""
        if ev.kind == "SL_HIT" and self._state in (AtrState.OPEN, AtrState.RUNNER):
            self._volume_steps = 0
            self._state = AtrState.CLOSED
            self._log("SL_HIT", price=ev.price_int)

    def on_bar(self, candle: Candle, signal: object) -> BarOutcome:
        """確定足ごとに TP1(半利+建値化)→ トレール → TP2(全決済)を処理する。"""
        if self._state in (AtrState.OPEN, AtrState.RUNNER):
            self._require_closed_bar(candle)
            touch = candle.h_int if self.direction > 0 else candle.l_int

            # OPEN: TP1 到達で半玉利確 + 残玉SLを建値へ前進 → RUNNER。
            if self._state is AtrState.OPEN:
                assert self._tp1_int is not None
                if self._profit_side(touch, self._tp1_int) >= 0:
                    if self._half_steps >= 1:
                        self._broker.close_volume(
                            client_order_id=self._oid("tp1_half"),
                            position_id=self.position_id, volume_steps=self._half_steps)
                        self._volume_steps -= self._half_steps
                        self._log("TP_CLOSE", level="tp1", closed=self._half_steps,
                                  runner=self._volume_steps)
                    self._state = AtrState.RUNNER
                    if self._entry_int is not None:
                        self._advance_sl_to(self._entry_int)   # 建値化(利益方向のみ)

            # RUNNER: 0.5×ATR トレール(利益方向のみ)→ TP2 到達で残玉全決済。
            if self._state is AtrState.RUNNER:
                assert self._tp2_int is not None and self._trail_distance is not None
                self._advance_sl_to(touch - self.direction * self._trail_distance)
                if self._profit_side(touch, self._tp2_int) >= 0:
                    self._broker.close_volume(
                        client_order_id=self._oid("tp2_close"),
                        position_id=self.position_id, volume_steps=self._volume_steps)
                    self._volume_steps = 0
                    self._state = AtrState.CLOSED
                    self._log("TP_CLOSE", level="tp2")

        return BarOutcome(closed=self._state is AtrState.CLOSED, expired=False)

    def close(self, reason: str) -> None:
        """残存ポジションの成行手仕舞い (データ末尾・シャットダウン)。"""
        if self._state in (AtrState.OPEN, AtrState.RUNNER):
            self._broker.close_volume(
                client_order_id=self._oid("close_all"),
                position_id=self.position_id, volume_steps=self._volume_steps)
            self._volume_steps = 0
            self._state = AtrState.CLOSED
            self._log("CLOSE_ALL", reason=reason)

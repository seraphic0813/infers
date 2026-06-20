"""smc_bos 執行モデル: 成行参入 + 固定SL/RR利確 + SL前進 (L2 / 段階S4 / spec.md §3.4)。

`core.execution.ExecutionModel` を構造的に充足する執行モデル:

  - place(): 即時に成行で参入し、固定SLを発注時に設定する (SLなしの状態は作らない)。
  - on_bar(): 確定足ごとに ①SL前進(be_mode別。spec.md §3.3)②高値/安値が
    固定TPへ到達したら全決済 (確定足主義)。
  - on_broker_event(): ブローカーのSLヒットで CLOSED へ。
  - close(): 残存ポジションを成行手仕舞い (データ末尾・シャットダウン)。

`be_mode`(spec.md §3.3。**既定 `off`**):
  - `off`(既定): SL前進なし。XAUUSD M30・5年の実測比較(spec.md §3.3 追記
    2026-06-20)で `at_1r`/`structure` を一貫して上回った(PF1.456 vs 1.384/1.042、
    純益+$4,258 vs +$2,340/+$145)。早期建値化が「勝ちトレードを伸ばす前に
    引き上げる」副作用を持つことは Narrow Focus(entry-methodology.md。
    含み益トリガー禁止の理由そのもの)でも確認済みの傾向で、SMCでも再現された。
  - `at_1r`(原典準拠・opt-in): 確定足の高値(買い)/安値(売り)が1R価格水準
    (= 参入参考価格 + 方向×初期リスク幅)へ到達したら、SLを実約定価格(建値)へ
    前進する。トリガーは確定足の価格水準到達であり、PnL/pips の評価ではない
    (§A-2確定足主義に準拠。§A-4改訂によりSMCの手法契約として許可された
    含み益トリガーの具体的な判定方法)。実測では `off` に劣後(上記)。
  - `structure`(代替・opt-in): `signal`(SmcBosProvider が出す SmcOutput)の
    `swing_low_int`(買い)/`swing_high_int`(売り)へSLを前進する。実測では
    勝率・DDは改善するが PF が1.04まで低下し純益はほぼゼロ(上記)。

いずれのモードも `_advance_sl_to` が一元的にSL変更を仲介し、**利益方向への
改善でなければ無条件で no-op**(例外を投げずに単に無視)とすることで
SL単調性(A-3)を構造的に保証する(narrow_focus の `move_sl` とは異なり、
SMCは同一トリガー条件が複数バーに渡って継続する一方で再前進が冪等で
あることを要求するため、例外ではなく no-op を選んだ)。

安全原則 (CLAUDE.md §A) は本モデルにも等しく強制される:
  - A-1 LLM非依存 — 本モジュールは LLM を一切呼ばない (防御は決定論)。
  - A-2 確定足主義 — on_bar は確定足のみ受領する。
  - A-3 SL単調性 — `_advance_sl_to` が利益方向への改善のみを許可する。
  - A-4 防御トリガーは手法契約 — SMCは含み益(1R)トリガーを自手法の契約として
    採用(`be_mode=at_1r`)。Narrow Focus はこの自由を使わない(CLAUDE.md A-4)。
  - A-9 冪等性 — 全注文に決定論的 client_order_id を付与する。
"""

from __future__ import annotations

from enum import Enum, auto
from typing import Callable

from infers.core.execution import BarOutcome, BrokerPort
from infers.core.models import Candle


_BE_MODES = ("off", "at_1r", "structure")


class SmcState(Enum):
    IDLE = auto()      # 未参入
    OPEN = auto()      # 成行約定済み (TP/SL待ち)
    CLOSED = auto()    # 終了 (TP到達・SLヒット・強制手仕舞い)


class SmcExecution:
    """成行参入 + 固定SL/RR利確 + SL前進の執行モデル(ExecutionModel を構造的に充足)。"""

    def __init__(self, *, position_id: str, direction: int,
                 broker: BrokerPort, config: object = None,
                 journal_sink: Callable[[str, dict], None] | None = None,
                 be_mode: str = "off") -> None:
        if direction not in (+1, -1):
            raise ValueError("direction must be +1 or -1")
        if be_mode not in _BE_MODES:
            raise ValueError(f"be_mode must be one of {_BE_MODES}, got {be_mode!r}")
        self.position_id = position_id
        self.direction = direction
        self._broker = broker
        # config は受け取るが本モデルでは未使用 (TP/SL/数量は intent から取る)。
        # TradingLoop の生成器シグネチャ互換のために受ける。
        self._journal_sink = journal_sink
        self._be_mode = be_mode

        self._state = SmcState.IDLE
        self._seq = 0
        self._sl_int: int | None = None
        self._tp_int: int | None = None
        self._entry_int: int | None = None
        self._be_trigger_price: int | None = None   # be_mode=at_1r の1R価格水準
        self._volume_steps = 0
        self.journal: list[tuple[str, dict]] = []
        # 可視化フック互換 (recorder は execution の plan を参照しうる)。
        self.plan: object | None = None

    # -- 参照 (ExecutionModel 抽象) ------------------------------------------------

    @property
    def state(self) -> SmcState:
        return self._state

    @property
    def volume_steps(self) -> int:
        return self._volume_steps

    @property
    def closed(self) -> bool:
        return self._state is SmcState.CLOSED

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

    # -- ExecutionModel 抽象 -------------------------------------------------------

    def place(self, intent: object) -> None:
        """成行参入。SLは発注と同時に必ず設定する。

        intent は duck-typed (limit_price_int=参入参考価格 / sl_int / volume_steps /
        fib_target_int=固定TP)。TradePlan をそのまま受け取れる。
        """
        if self._state is not SmcState.IDLE:
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
        self._state = SmcState.OPEN
        # 1R価格水準 = 参入参考価格(plan基準) + 方向×初期リスク幅(spec.md §3.3)。
        # 実約定価格(spread込み)ではなくplanの参考値で固定する(TP同様、planが
        # 確定したら以後はplan由来の絶対価格水準のみで判定する設計)。
        risk_ticks = abs(intent.limit_price_int - intent.sl_int)
        self._be_trigger_price = intent.limit_price_int + self.direction * risk_ticks
        self._log("MARKET_ENTRY", fill=fill, sl=self._sl_int, tp=self._tp_int,
                  volume=intent.volume_steps, be_trigger=self._be_trigger_price)

    def on_broker_event(self, ev: object) -> None:
        """ブローカーのSLヒットで CLOSED へ (成行参入は別途FILLイベントを伴わない)。"""
        if ev.kind == "SL_HIT" and self._state is SmcState.OPEN:
            self._volume_steps = 0
            self._state = SmcState.CLOSED
            self._log("SL_HIT", price=ev.price_int)

    def on_bar(self, candle: Candle, signal: object) -> BarOutcome:
        """確定足ごとに ①SL前進(be_mode別)②固定TP到達判定の順で処理する。"""
        if self._state is SmcState.OPEN and self._tp_int is not None:
            self._require_closed_bar(candle)
            self._advance_sl(candle, signal)
            touch = candle.h_int if self.direction > 0 else candle.l_int
            if self._profit_side(touch, self._tp_int) >= 0:
                self._broker.close_volume(
                    client_order_id=self._oid("tp_close"),
                    position_id=self.position_id, volume_steps=self._volume_steps)
                self._volume_steps = 0
                self._state = SmcState.CLOSED
                self._log("TP_CLOSE", tp=self._tp_int)
        return BarOutcome(closed=self._state is SmcState.CLOSED, expired=False)

    def _advance_sl(self, candle: Candle, signal: object) -> None:
        """be_mode に応じたSL前進トリガーを評価する(spec.md §3.3)。"""
        if self._be_mode == "off":
            return
        if self._be_mode == "at_1r":
            if self._be_trigger_price is None or self._entry_int is None:
                return
            touched = (candle.h_int >= self._be_trigger_price if self.direction > 0
                      else candle.l_int <= self._be_trigger_price)
            if touched:
                self._advance_sl_to(self._entry_int)
        else:  # structure
            candidate = (getattr(signal, "swing_low_int", None) if self.direction > 0
                        else getattr(signal, "swing_high_int", None))
            if candidate is not None:
                self._advance_sl_to(candidate)

    def _advance_sl_to(self, target: int) -> None:
        """SLを target へ前進する。利益方向への改善でなければ no-op(A-3 SL単調性)。

        narrow_focus の `move_sl` は逆行を例外で拒否するが、本モデルは同一の
        前進トリガーが複数バーに渡って継続する(冪等な再前進が前提)ため、
        改善が無ければ静かに無視する設計にする。
        """
        assert self._sl_int is not None
        if self._profit_side(target, self._sl_int) <= 0:
            return
        self._broker.modify_sl(position_id=self.position_id, new_sl_int=target)
        self._sl_int = target
        self._log("MOVE_SL", new_sl=target)

    def close(self, reason: str) -> None:
        """残存ポジションの成行手仕舞い (データ末尾・シャットダウン)。"""
        if self._state is SmcState.OPEN:
            self._broker.close_volume(
                client_order_id=self._oid("close_all"),
                position_id=self.position_id, volume_steps=self._volume_steps)
            self._volume_steps = 0
            self._state = SmcState.CLOSED
            self._log("CLOSE_ALL", reason=reason)

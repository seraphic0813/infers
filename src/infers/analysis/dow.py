"""ダウ理論 状態機械 (設計書 §3.2)。

確定済み SwingPoint 列から HH/HL/LH/LL の StructureEvent を生成し、
トレンド状態を遷移させる。発行されるイベントのうち
「安値切り上げ確定 (HL)」「高値更新確定 (HH)」は、エントリー根拠と
建値SL移動 (設計書 §6.3) の両方が購読する。

転換の定義 (マニュアル 2.1):
  上昇トレンドの転換 = 高値更新失敗 (LH) かつ 安値切り下げ (LL) の両成立。
  片方のみでは *_SUSPECT (警戒) に留まり、HH (高値更新) で警戒解除する。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Literal

from infers.analysis.zigzag import SwingPoint


class StructureEventType(Enum):
    HH = auto()   # 高値更新
    HL = auto()   # 安値切り上げ
    LH = auto()   # 高値切り下げ (更新失敗。同値も失敗とみなす)
    LL = auto()   # 安値切り下げ


class TrendState(Enum):
    UP = auto()
    UP_SUSPECT = auto()      # 上昇継続中だが転換条件の片方が成立
    DOWN = auto()
    DOWN_SUSPECT = auto()
    UNDEFINED = auto()       # 初期状態 (HH/HL or LH/LL のペア成立まで)


DowFamily = Literal["ALIGNED", "REVERSAL", "NEUTRAL", "CONFLICT"]


def classify_dow(state: TrendState, direction: int) -> DowFamily:
    """ミクロのダウ状態を、狙う方向に対する「Dow Family」判定へ写像する (手法G2-⑧③)。

    買い (direction>0) の場合:
      - UP           → ALIGNED  (完全な順行。強度 高)
      - DOWN_SUSPECT → REVERSAL (下降が安値切り上げ=反転の初動。第2波→第3波。強度 中)
      - UP_SUSPECT   → NEUTRAL  (上昇の勢い減退。加点なし・vetoなし)
      - UNDEFINED    → NEUTRAL  (方向未確定。加点なし・vetoなし)
      - DOWN         → CONFLICT (明確な下降 = 落ちるナイフ。クラスタ破壊)
    売り (direction<0) は UP↔DOWN・*_SUSPECT を反転した対称判定。
    """
    if direction not in (+1, -1):
        raise ValueError("direction must be +1 or -1")
    up_side = direction > 0
    if state is (TrendState.UP if up_side else TrendState.DOWN):
        return "ALIGNED"
    if state is (TrendState.DOWN_SUSPECT if up_side else TrendState.UP_SUSPECT):
        return "REVERSAL"
    if state is (TrendState.DOWN if up_side else TrendState.UP):
        return "CONFLICT"
    return "NEUTRAL"


@dataclass(frozen=True)
class StructureEvent:
    """HH/HL/LH/LL イベント。ジャーナルへそのまま記録する (CLAUDE.md 第11条)。"""

    type: StructureEventType
    swing: SwingPoint         # 今回の確定スイング
    prev_swing: SwingPoint    # 比較対象 (直前の同種スイング)
    state_after: TrendState   # このイベント処理後のトレンド状態


class DowStateMachine:
    """単一 (symbol, tf) 系列のダウ理論FSM。

    入力は ZigZagDetector が確定させた SwingPoint のみ (確定足主義の連鎖)。
    同種の先行スイングが存在しない間はイベントを発行しない。
    """

    def __init__(self) -> None:
        self._state = TrendState.UNDEFINED
        self._last_high: SwingPoint | None = None
        self._last_low: SwingPoint | None = None
        self._last_high_event: StructureEventType | None = None
        self._last_low_event: StructureEventType | None = None

    @property
    def state(self) -> TrendState:
        return self._state

    @property
    def last_high(self) -> SwingPoint | None:
        return self._last_high

    @property
    def last_low(self) -> SwingPoint | None:
        return self._last_low

    def on_swing(self, swing: SwingPoint) -> StructureEvent | None:
        """確定スイングを1点処理し、イベントが生成された場合のみ返す。"""
        if swing.kind == "HIGH":
            prev, self._last_high = self._last_high, swing
            if prev is None:
                return None
            # 同値は「更新失敗」とみなす (厳密大なり比較)
            ev_type = StructureEventType.HH if swing.price_int > prev.price_int else StructureEventType.LH
            self._last_high_event = ev_type
        else:
            prev, self._last_low = self._last_low, swing
            if prev is None:
                return None
            ev_type = StructureEventType.LL if swing.price_int < prev.price_int else StructureEventType.HL
            self._last_low_event = ev_type

        self._state = self._transition(ev_type)
        return StructureEvent(type=ev_type, swing=swing, prev_swing=prev, state_after=self._state)

    # -- 遷移関数 (設計書 §3.2 の遷移表 + 対称補完) ----------------------------

    def _transition(self, ev: StructureEventType) -> TrendState:
        s = self._state
        E = StructureEventType

        if s == TrendState.UNDEFINED:
            # ブートストラップ: HH+HL の両成立で UP、LH+LL で DOWN
            if self._last_high_event == E.HH and self._last_low_event == E.HL:
                return TrendState.UP
            if self._last_high_event == E.LH and self._last_low_event == E.LL:
                return TrendState.DOWN
            return TrendState.UNDEFINED

        if s == TrendState.UP:
            if ev in (E.LH, E.LL):
                return TrendState.UP_SUSPECT   # 転換条件の片方が成立 → 警戒
            return TrendState.UP

        if s == TrendState.UP_SUSPECT:
            if ev == E.HH:
                return TrendState.UP           # 高値更新 → 警戒解除
            if ev == E.LL and self._last_high_event == E.LH:
                return TrendState.DOWN         # 更新失敗 + 安値切り下げ = 転換確定
            if ev == E.LH and self._last_low_event == E.LL:
                return TrendState.DOWN
            return TrendState.UP_SUSPECT

        if s == TrendState.DOWN:
            if ev in (E.HL, E.HH):
                return TrendState.DOWN_SUSPECT
            return TrendState.DOWN

        # DOWN_SUSPECT
        if ev == E.LL:
            return TrendState.DOWN             # 安値更新 → 警戒解除
        if ev == E.HH and self._last_low_event == E.HL:
            return TrendState.UP               # 転換確定
        if ev == E.HL and self._last_high_event == E.HH:
            return TrendState.UP
        return TrendState.DOWN_SUSPECT

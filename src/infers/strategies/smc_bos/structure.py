"""smc_bos 手法固有の構造検出: フラクタル/ピボット型スイング + BOS判定
(L2 / spec.md §2.2・§5.4)。

スイングは「ある確定足の左右 lookback 本がすべてそれより低い(高い)」
フラクタル/ピボット型で検出する。確定は右側 lookback 本目の確定足クローズ
時点(確定前の候補は外部に公開しない。CLAUDE.md 第2条: リペイント禁止)。

出典EA の `FindSwings`(単純な N 本前の high/low)はダマシに弱いため採用せず、
この確定遅延つきピボット検出を用いる(spec.md §2.2)。既存の
`strategies/narrow_focus/zigzag.py`(閾値反転型ZigZag)とは判定方式が異なり、
かつ他手法フォルダへの L2→L2 依存を避けるため本手法専用に自前実装する
(spec.md §5.4)。

CHoCH(Change of Character)は段階2(S5)で検討する補助シグナルのため、
本モジュールでは未実装(spec.md §2.3)。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from infers.core.models import Candle, Timeframe


@dataclass(frozen=True)
class SwingPoint:
    """確定済みスイングポイント。confirmed_at 以降にのみ判断材料として使用できる。"""

    kind: Literal["HIGH", "LOW"]
    bar_time: datetime        # 極値を付けたバーの open_time (UTC)
    price_int: int            # 整数ティック
    tf: Timeframe
    confirmed_at: datetime    # 確定したバー(右側確認バー)の close_time (UTC)


class SwingDetector:
    """確定足を1本ずつ受け取り、フラクタル/ピボット型のスイングを返す。

    バー i が「スイング高値」になるのは、左右 lookback 本の高値がすべて
    バー i の高値より低い場合(安値は対称)。厳密な不等号 (`<`) を使うため、
    隣接バーとの完全同値はピボットとみなさない(曖昧な確定を避ける保守的選択)。
    """

    def __init__(self, lookback: int) -> None:
        if lookback < 1:
            raise ValueError("lookback must be >= 1")
        self.lookback = lookback
        self._window = 2 * lookback + 1
        self._buf: deque[Candle] = deque(maxlen=self._window)
        self._key: tuple[str, Timeframe] | None = None
        self._last_time: datetime | None = None

    def update(self, candle: Candle) -> list[SwingPoint]:
        """確定足を1本処理し、この足で確定したスイング(0〜2個)を返す。"""
        if not candle.is_closed:
            raise ValueError("SwingDetector accepts closed candles only (CLAUDE.md rule 2)")
        if self._key is None:
            self._key = (candle.symbol, candle.tf)
        elif self._key != (candle.symbol, candle.tf):
            raise ValueError(f"mixed series: expected {self._key}")
        if self._last_time is not None and candle.open_time <= self._last_time:
            raise ValueError("candles must be strictly increasing in open_time")
        self._last_time = candle.open_time

        self._buf.append(candle)
        if len(self._buf) < self._window:
            return []

        center = self._buf[self.lookback]
        others = [c for i, c in enumerate(self._buf) if i != self.lookback]
        swings: list[SwingPoint] = []
        if all(c.h_int < center.h_int for c in others):
            swings.append(SwingPoint(kind="HIGH", bar_time=center.open_time,
                                     price_int=center.h_int, tf=center.tf,
                                     confirmed_at=candle.close_time))
        if all(c.l_int > center.l_int for c in others):
            swings.append(SwingPoint(kind="LOW", bar_time=center.open_time,
                                     price_int=center.l_int, tf=center.tf,
                                     confirmed_at=candle.close_time))
        return swings


def bos_direction(close_int: int, *, swing_high: int | None, swing_low: int | None,
                  buffer_ticks: int) -> int:
    """終値が直近の確定スイングをバッファ込みでブレイクしたか (spec.md §2.2)。

    買い = +1 (終値 > swing_high + buffer) / 売り = -1 (終値 < swing_low - buffer) /
    不成立 = 0。スイングが未確定 (None) の方向は判定不能として無視する。
    """
    if buffer_ticks < 0:
        raise ValueError("buffer_ticks must be >= 0")
    if swing_high is not None and close_int > swing_high + buffer_ticks:
        return +1
    if swing_low is not None and close_int < swing_low - buffer_ticks:
        return -1
    return 0

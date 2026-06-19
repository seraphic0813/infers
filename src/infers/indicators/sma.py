"""単純移動平均 (設計書 §4.1)。"""

from __future__ import annotations

from collections import deque
from decimal import Decimal

from infers.indicators._common import Q


class SMA:
    """単純移動平均 (90SMA / 200SMA 用)。入力は終値の整数ティック。"""

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self._buf: deque[int] = deque()
        self._sum: int = 0
        self._value: Decimal | None = None

    @property
    def is_ready(self) -> bool:
        return self._value is not None

    @property
    def value(self) -> Decimal | None:
        """現在のSMA値(ティック単位の Decimal)。ウォームアップ中は None。"""
        return self._value

    def update(self, close_int: int) -> Decimal | None:
        self._buf.append(close_int)
        self._sum += close_int
        if len(self._buf) > self.period:
            self._sum -= self._buf.popleft()
        if len(self._buf) < self.period:
            return None
        self._value = (Decimal(self._sum) / self.period).quantize(Q)
        return self._value

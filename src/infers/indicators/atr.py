"""Average True Range (Wilder) (設計書 §4.1)。"""

from __future__ import annotations

from decimal import Decimal

from infers.indicators._common import Q, wilder_smooth


class ATR:
    """Average True Range (Wilder)。入力は h/l/c の整数ティック。

    シード: 最初の period 本の TR の単純平均。以後 Wilder 平滑。
    """

    def __init__(self, period: int = 14) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self._prev_close: int | None = None
        self._seed_trs: list[int] = []
        self._value: Decimal | None = None

    @property
    def is_ready(self) -> bool:
        return self._value is not None

    @property
    def value(self) -> Decimal | None:
        return self._value

    def update(self, h_int: int, l_int: int, c_int: int) -> Decimal | None:
        if h_int < l_int:
            raise ValueError(f"high {h_int} < low {l_int}")
        if self._prev_close is None:
            tr = h_int - l_int  # 初回バーは前終値なし
        else:
            tr = max(
                h_int - l_int,
                abs(h_int - self._prev_close),
                abs(l_int - self._prev_close),
            )
        self._prev_close = c_int

        if self._value is None:
            self._seed_trs.append(tr)
            if len(self._seed_trs) < self.period:
                return None
            self._value = (Decimal(sum(self._seed_trs)) / self.period).quantize(Q)
            self._seed_trs.clear()
            return self._value

        self._value = wilder_smooth(self._value, Decimal(tr), self.period)
        return self._value

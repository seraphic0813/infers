"""指数移動平均 (EMA) (smc_bos 手法の EMA80 フィルタ用 / spec.md §5.3)。"""

from __future__ import annotations

from decimal import Decimal

from infers.indicators._common import Q


class EMA:
    """指数移動平均。入力は終値の整数ティック。

    シード: 最初の period 本の単純移動平均(SMA)で初期化する。以後は
    漸化式 EMA_t = EMA_{t-1} + k*(price - EMA_{t-1}), k = 2/(period+1) で
    更新する。k は固定量子化 Decimal で、浮動小数を一切使わない
    (CLAUDE.md 第5条: float禁止。プラットフォーム非依存の決定論性)。
    """

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self._k = (Decimal(2) / (period + 1)).quantize(Q)
        self._seed_sum: int = 0
        self._seed_count: int = 0
        self._value: Decimal | None = None

    @property
    def is_ready(self) -> bool:
        return self._value is not None

    @property
    def value(self) -> Decimal | None:
        """現在のEMA値(ティック単位の Decimal)。ウォームアップ中は None。"""
        return self._value

    def update(self, close_int: int) -> Decimal | None:
        if self._value is not None:
            self._value = (self._value + self._k * (Decimal(close_int) - self._value)).quantize(Q)
            return self._value

        # --- シードフェーズ: 最初の period 本の SMA ---
        self._seed_sum += close_int
        self._seed_count += 1
        if self._seed_count < self.period:
            return None
        self._value = (Decimal(self._seed_sum) / self.period).quantize(Q)
        return self._value

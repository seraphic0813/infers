"""RSI (Wilder, 期間14) — 内部状態を公開し前進計算を純粋関数で提供 (設計書 §4.2)。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from infers.indicators._common import Q, wilder_smooth


@dataclass(frozen=True)
class RsiState:
    """Wilder RSI の完全な内部状態 (設計書 §4.2)。

    未来裁量エンジンはこのスナップショットから rsi_forward() で
    任意の仮想終値パスを前進計算する (設計書 §5.3)。
    """

    period: int
    avg_gain: Decimal      # ティック単位
    avg_loss: Decimal      # ティック単位
    last_close_int: int

    @property
    def rsi(self) -> Decimal:
        return rsi_value(self.avg_gain, self.avg_loss)


def rsi_value(avg_gain: Decimal, avg_loss: Decimal) -> Decimal:
    """RSI = 100 * g / (g + l)。両者ゼロ(完全フラット)は慣例的に50。"""
    total = avg_gain + avg_loss
    if total == 0:
        return Decimal(50)
    return (Decimal(100) * avg_gain / total).quantize(Q)


def rsi_forward(state: RsiState, closes: Sequence[int]) -> tuple[Decimal, RsiState]:
    """純粋関数: 状態 state から終値列 closes を前進計算する。

    引数の state は変更しない。実測の確定足にも仮想の未来パスにも
    同一コードで適用できる (確定足主義と未来裁量の共通基盤)。
    """
    g, l, prev = state.avg_gain, state.avg_loss, state.last_close_int
    for c in closes:
        delta = c - prev
        gain = Decimal(delta) if delta > 0 else Decimal(0)
        loss = Decimal(-delta) if delta < 0 else Decimal(0)
        g = wilder_smooth(g, gain, state.period)
        l = wilder_smooth(l, loss, state.period)
        prev = c
    new_state = RsiState(period=state.period, avg_gain=g, avg_loss=l, last_close_int=prev)
    return new_state.rsi, new_state


class WilderRSI:
    """ステートフルRSI。確定足の終値(整数ティック)を1本ずつ受け取る。

    シード: 最初の period 個の前日比から単純平均で avg_gain/avg_loss を
    初期化。以後の更新は rsi_forward() (純粋関数) に委譲し、
    ライブ更新と未来シミュレーションの計算経路を完全一致させる。
    """

    def __init__(self, period: int = 14) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.period = period
        self._prev_close: int | None = None
        self._seed_gains: list[int] = []
        self._seed_losses: list[int] = []
        self._state: RsiState | None = None

    @property
    def is_ready(self) -> bool:
        return self._state is not None

    @property
    def state(self) -> RsiState | None:
        """未来裁量エンジンへ渡す内部状態スナップショット(イミュータブル)。"""
        return self._state

    @property
    def value(self) -> Decimal | None:
        return self._state.rsi if self._state is not None else None

    def update(self, close_int: int) -> Decimal | None:
        if self._state is not None:
            rsi, self._state = rsi_forward(self._state, [close_int])
            return rsi

        # --- シードフェーズ ---
        if self._prev_close is not None:
            delta = close_int - self._prev_close
            self._seed_gains.append(max(delta, 0))
            self._seed_losses.append(max(-delta, 0))
        self._prev_close = close_int

        if len(self._seed_gains) < self.period:
            return None

        self._state = RsiState(
            period=self.period,
            avg_gain=(Decimal(sum(self._seed_gains)) / self.period).quantize(Q),
            avg_loss=(Decimal(sum(self._seed_losses)) / self.period).quantize(Q),
            last_close_int=close_int,
        )
        self._seed_gains.clear()
        self._seed_losses.clear()
        return self._state.rsi

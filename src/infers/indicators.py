"""インジケーター・コア (設計書 §4.1〜4.2 / CLAUDE.md 第6条)。

- 入力価格はすべて整数ティック (int)。float は受け取らない。
- 導出値 (SMA/ATR/RSI) は固定量子化 Decimal (1e-9) で表現する。
  浮動小数を使わず、量子化を毎ステップ行うことでプラットフォーム
  非依存の決定論性 (バックテスト⇄ライブの同一性) を保証する。
- Wilder平滑の内部状態は frozen dataclass + 純粋関数で公開し、
  未来裁量エンジン (設計書 §5.3) の前進計算・逆算の基盤とする。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

# 固定量子化単位。すべての導出値はこの粒度に丸める(決定論性の要)。
Q = Decimal("0.000000001")  # 1e-9


def _wilder_smooth(prev: Decimal, x: Decimal, period: int) -> Decimal:
    """Wilder平滑 1ステップ: avg' = (avg*(n-1) + x) / n を固定量子化で。"""
    return ((prev * (period - 1) + x) / period).quantize(Q)


# ---------------------------------------------------------------------------
# SMA
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# ATR (Wilder)
# ---------------------------------------------------------------------------

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

        self._value = _wilder_smooth(self._value, Decimal(tr), self.period)
        return self._value


# ---------------------------------------------------------------------------
# RSI (Wilder, 期間14) — 内部状態を公開し前進計算を純粋関数で提供
# ---------------------------------------------------------------------------

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
        g = _wilder_smooth(g, gain, state.period)
        l = _wilder_smooth(l, loss, state.period)
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

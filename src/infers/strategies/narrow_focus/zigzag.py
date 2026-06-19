"""確定遅延つきZigZagスイング検出 (設計書 §3.1 / CLAUDE.md 第2条)。

スイングは「直近極値から逆方向に reversal_ticks 以上動いた確定足の
クローズ時点」で初めて確定する。この確定遅延は仕様であり、ノイズ除去
機能そのもの (建値SL移動のダウ理論ベース判定が意図的に利用する)。

リペイント禁止: 一度確定した SwingPoint は不変 (frozen)。確定前の
候補極値は外部に公開しない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from infers.core.models import Candle, Timeframe


@dataclass(frozen=True)
class SwingPoint:
    """確定済みスイングポイント (設計書 §3.1)。

    confirmed_at 以降にのみ判断材料として使用できる(リペイント防止)。
    """

    kind: Literal["HIGH", "LOW"]
    bar_time: datetime        # 極値を付けたバーの open_time (UTC)
    price_int: int            # 整数ティック
    tf: Timeframe
    confirmed_at: datetime    # 確定したバーの close_time (UTC)


class ZigZagDetector:
    """確定足を1本ずつ受け取り、確定したスイングを返す。

    reversal_ticks (反転閾値) は設計書 §3.1 の
    θ_rev = max(k_atr×ATR, k_pct×price) を呼び出し側 (特徴量エンジン) が
    計算して渡す。本クラスは閾値の出所に関知しない(純粋性の維持)。
    update() ごとに閾値を上書きできるため、ATR連動の動的閾値に対応する。
    """

    def __init__(self, reversal_ticks: int) -> None:
        if reversal_ticks < 1:
            raise ValueError("reversal_ticks must be >= 1")
        self._default_theta = reversal_ticks

        # 追跡方向: None=初期化中 / "UP"=高値候補を追跡中 / "DOWN"=安値候補を追跡中
        self._dir: Literal["UP", "DOWN"] | None = None
        self._cand_high: int | None = None
        self._cand_high_time: datetime | None = None
        self._cand_low: int | None = None
        self._cand_low_time: datetime | None = None
        self._last_time: datetime | None = None
        self._key: tuple[str, Timeframe] | None = None  # (symbol, tf) 固定

    def update(self, candle: Candle, *, reversal_ticks: int | None = None) -> SwingPoint | None:
        """確定足を1本処理し、スイングが確定した場合のみ返す(高々1個/バー)。"""
        # --- 入力契約の検証 (確定足主義・時系列単調・同一系列) ---
        if not candle.is_closed:
            raise ValueError("ZigZag accepts closed candles only (CLAUDE.md rule 2)")
        if self._key is None:
            self._key = (candle.symbol, candle.tf)
        elif self._key != (candle.symbol, candle.tf):
            raise ValueError(f"mixed series: expected {self._key}")
        if self._last_time is not None and candle.open_time <= self._last_time:
            raise ValueError("candles must be strictly increasing in open_time")
        self._last_time = candle.open_time

        theta = self._default_theta if reversal_ticks is None else reversal_ticks
        if theta < 1:
            raise ValueError("reversal_ticks must be >= 1")

        if self._dir is None:
            return self._update_init(candle, theta)
        if self._dir == "UP":
            return self._update_tracking_high(candle, theta)
        return self._update_tracking_low(candle, theta)

    # -- 内部状態遷移 ---------------------------------------------------------

    def _update_init(self, c: Candle, theta: int) -> SwingPoint | None:
        """初期化: 最初の方向が定まるまで高値・安値候補を両追跡する。"""
        if self._cand_high is None or c.h_int > self._cand_high:
            self._cand_high, self._cand_high_time = c.h_int, c.open_time
        if self._cand_low is None or c.l_int < self._cand_low:
            self._cand_low, self._cand_low_time = c.l_int, c.open_time

        assert self._cand_high is not None and self._cand_low is not None
        if self._cand_high - c.l_int >= theta:
            # 高値候補から θ 下落 → 高値候補が最初の確定スイングHIGH
            swing = self._confirm("HIGH", self._cand_high, self._cand_high_time, c)
            self._dir = "DOWN"
            self._cand_low, self._cand_low_time = c.l_int, c.open_time
            return swing
        if c.h_int - self._cand_low >= theta:
            swing = self._confirm("LOW", self._cand_low, self._cand_low_time, c)
            self._dir = "UP"
            self._cand_high, self._cand_high_time = c.h_int, c.open_time
            return swing
        return None

    def _update_tracking_high(self, c: Candle, theta: int) -> SwingPoint | None:
        assert self._cand_high is not None
        if c.h_int > self._cand_high:
            self._cand_high, self._cand_high_time = c.h_int, c.open_time
        if self._cand_high - c.l_int >= theta:
            swing = self._confirm("HIGH", self._cand_high, self._cand_high_time, c)
            self._dir = "DOWN"
            self._cand_low, self._cand_low_time = c.l_int, c.open_time
            return swing
        return None

    def _update_tracking_low(self, c: Candle, theta: int) -> SwingPoint | None:
        assert self._cand_low is not None
        if c.l_int < self._cand_low:
            self._cand_low, self._cand_low_time = c.l_int, c.open_time
        if c.h_int - self._cand_low >= theta:
            swing = self._confirm("LOW", self._cand_low, self._cand_low_time, c)
            self._dir = "UP"
            self._cand_high, self._cand_high_time = c.h_int, c.open_time
            return swing
        return None

    def _confirm(
        self, kind: Literal["HIGH", "LOW"], price_int: int | None,
        bar_time: datetime | None, confirming: Candle,
    ) -> SwingPoint:
        assert price_int is not None and bar_time is not None
        return SwingPoint(
            kind=kind,
            bar_time=bar_time,
            price_int=price_int,
            tf=confirming.tf,
            confirmed_at=confirming.close_time,  # 確定遅延: このバーのクローズで確定
        )

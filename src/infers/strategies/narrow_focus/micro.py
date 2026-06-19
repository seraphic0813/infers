"""ミクロ分析: グランビルの法則の数値化と RSI 極値判定 (設計書 §4.1〜4.2)。

裁量表現「移動平均まで下落して反発」等は、ATR正規化乖離 d と
SMA正規化傾き slope で形式化する (CLAUDE.md 第6条: 計算はすべて
整数ティック入力 + 固定量子化 Decimal)。

  d(t)     = (close − SMA) / ATR
  slope(t) = (SMA(t) − SMA(t−n)) / (n × ATR)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from infers.indicators import Q
from infers.core.models import Candle

GranvilleKind = Literal["BUY2", "BUY3", "BUY4", "SELL2", "SELL3", "SELL4"]

# 既定パラメータ (設計書 §4.1。バックテストで調整する)
THETA_TOUCH = Decimal("0.3")    # SMA「接触」とみなす正規化乖離
THETA_PIERCE = Decimal("0.8")   # 買③で許容する一時的な割り込み深さ
THETA_FAR = Decimal("3.0")      # 買④/売④の「大幅乖離」


def normalized_deviation(price_int: int, sma: Decimal, atr: Decimal) -> Decimal:
    """ATR正規化乖離 d。atr は正の Decimal (ティック単位) であること。"""
    if atr <= 0:
        raise ValueError("atr must be positive")
    return ((Decimal(price_int) - sma) / atr).quantize(Q)


def sma_slope(sma_now: Decimal, sma_prev: Decimal, atr: Decimal, n: int) -> Decimal:
    """n 本前との差による正規化傾き。"""
    if atr <= 0 or n < 1:
        raise ValueError("atr must be positive and n >= 1")
    return ((sma_now - sma_prev) / (n * atr)).quantize(Q)


@dataclass(frozen=True)
class GranvilleSignal:
    """グランビルサイン成立イベント (確定足クローズ時点で発行)。"""

    kind: GranvilleKind
    direction: int              # +1 買い / -1 売り
    bar_time: datetime          # 成立した確定足の open_time
    sma_period: int             # 90 or 200 (どのSMAに対するサインか)
    d_close: Decimal            # 成立時の正規化乖離 (ジャーナル用)


class GranvilleDetector:
    """単一 (symbol, tf, SMA期間) 系列のグランビル判定器。

    毎確定足ごとに update() を呼ぶ。SMA/ATR/slope の計算は呼び出し側
    (特徴量エンジン) の責務で、ウォームアップ完了後の値のみを渡すこと。
    """

    def __init__(
        self,
        sma_period: int,
        *,
        theta_touch: Decimal = THETA_TOUCH,
        theta_pierce: Decimal = THETA_PIERCE,
        theta_far: Decimal = THETA_FAR,
        lookback: int = 10,          # 買②/売②の「乖離からの接近」判定窓
    ) -> None:
        self.sma_period = sma_period
        self._touch = theta_touch
        self._pierce = theta_pierce
        self._far = theta_far
        self._d_hist: deque[Decimal] = deque(maxlen=lookback)
        self._prev: Candle | None = None

    def update(self, candle: Candle, sma: Decimal, atr: Decimal, slope: Decimal) -> list[GranvilleSignal]:
        """確定足を1本評価し、成立したサインを返す (複数同時成立あり)。"""
        if not candle.is_closed:
            raise ValueError("Granville accepts closed candles only (CLAUDE.md rule 2)")

        d_close = normalized_deviation(candle.c_int, sma, atr)
        d_low = normalized_deviation(candle.l_int, sma, atr)
        d_high = normalized_deviation(candle.h_int, sma, atr)
        prev = self._prev

        # 反転トリガーバー: 陽線反転 (終値が前バー高値を上抜く) / 陰線反転
        rev_bull = prev is not None and candle.c_int > candle.o_int and candle.c_int > prev.h_int
        rev_bear = prev is not None and candle.c_int < candle.o_int and candle.c_int < prev.l_int

        came_from_above = any(d > self._touch for d in self._d_hist)
        came_from_below = any(d < -self._touch for d in self._d_hist)

        signals: list[GranvilleSignal] = []

        def emit(kind: GranvilleKind, direction: int) -> None:
            signals.append(GranvilleSignal(
                kind=kind, direction=direction, bar_time=candle.open_time,
                sma_period=self.sma_period, d_close=d_close,
            ))

        # --- 買いサイン (slope >= 0 が前提。買④のみ乖離が支配条件) ---
        if slope >= 0:
            # 買②: 上から SMA まで下落後の再上昇 (接近 + 陽線反転)
            if came_from_above and abs(d_close) <= self._touch and rev_bull:
                emit("BUY2", +1)
            # 買③: SMA タッチ (許容割り込み内) からの反発、終値はSMA上
            if -self._pierce <= d_low <= self._touch and d_close > 0:
                emit("BUY3", +1)
        # 買④: 大幅下方乖離からの戻り (反転トリガー必須)
        if d_close <= -self._far and rev_bull:
            emit("BUY4", +1)

        # --- 売りサイン (対称) ---
        if slope <= 0:
            if came_from_below and abs(d_close) <= self._touch and rev_bear:
                emit("SELL2", -1)
            if -self._touch <= d_high <= self._pierce and d_close < 0:
                emit("SELL3", -1)
        if d_close >= self._far and rev_bear:
            emit("SELL4", -1)

        self._d_hist.append(d_close)
        self._prev = candle
        return signals


# ---------------------------------------------------------------------------
# RSI 極値 (マニュアル 3.2)
# ---------------------------------------------------------------------------

RSI_OVERSOLD = Decimal(30)
RSI_OVERBOUGHT = Decimal(70)

RsiExtreme = Literal["OVERSOLD", "OVERBOUGHT"]


def classify_rsi(rsi: Decimal) -> RsiExtreme | None:
    """現在値の極値分類。30以下=売られすぎ / 70以上=買われすぎ。"""
    if rsi <= RSI_OVERSOLD:
        return "OVERSOLD"
    if rsi >= RSI_OVERBOUGHT:
        return "OVERBOUGHT"
    return None


class RsiExtremeDetector:
    """極値圏への突入クロスを1回だけイベント化する (連発スパム防止)。

    圏内に留まる間は再発火せず、いったん圏外へ出てから再突入で発火する。
    """

    def __init__(self) -> None:
        self._prev_zone: RsiExtreme | None = None

    def update(self, rsi: Decimal) -> RsiExtreme | None:
        zone = classify_rsi(rsi)
        fired = zone if zone is not None and zone != self._prev_zone else None
        self._prev_zone = zone
        return fired


# RSI極値トリガーの既定有効時間窓 (手法G2-⑤ ④「突入イベント化」)。
# 突入したクロス足を含め、その後 RSI_EVENT_WINDOW 本まで根拠を有効とする
# (押し目/戻り完成時に極値から反発/反落していても窓の間は有効。0 で突入足のみ)。
RSI_EVENT_WINDOW = 3


class RsiExtremeRecency:
    """RSI極値の「突入イベント化」+ 有効時間窓 (手法G2-⑤ ②③④)。

    RSI が極値圏へ「上(下)から割り込んだ最初のクロス(突入)」を **1回だけ**
    イベント化し、突入足を含め window 本のあいだ active=True とする。意図は2つ:

      - **スパム防止 (④)**: 極値圏への長期滞在 (ベタ付き) でも再発火させず、
        単一イベントとして扱う。窓を過ぎれば圏内でも active=False になる。
      - **反発/反落の許容 (②③)**: 突入後に圏外へ戻っても (反発/反落)、窓の間は
        「≤30/≥70 に到達、またはそこから反発/反落」した有効根拠として扱う。

    確定足の RSI のみ update() する (リペイント禁止: CLAUDE.md 第2条)。
    買い (direction>0) は売られすぎ、売り (direction<0) は買われすぎを参照。
    """

    def __init__(self, window: int = RSI_EVENT_WINDOW) -> None:
        if window < 0:
            raise ValueError("window must be >= 0")
        self._window = window
        self._prev_oversold = False
        self._prev_overbought = False
        self._rem_oversold = 0          # 残り有効バー (>0 で active)
        self._rem_overbought = 0

    def update(self, rsi: Decimal) -> None:
        in_oversold = rsi <= RSI_OVERSOLD
        in_overbought = rsi >= RSI_OVERBOUGHT
        # 突入 (圏外→圏内のクロス) でのみ窓をリセット。圏内滞在中は再点火しない。
        if in_oversold and not self._prev_oversold:
            self._rem_oversold = self._window + 1
        elif self._rem_oversold > 0:
            self._rem_oversold -= 1
        if in_overbought and not self._prev_overbought:
            self._rem_overbought = self._window + 1
        elif self._rem_overbought > 0:
            self._rem_overbought -= 1
        self._prev_oversold = in_oversold
        self._prev_overbought = in_overbought

    def active(self, direction: int) -> bool:
        if direction not in (+1, -1):
            raise ValueError("direction must be +1 or -1")
        return (self._rem_oversold if direction > 0 else self._rem_overbought) > 0

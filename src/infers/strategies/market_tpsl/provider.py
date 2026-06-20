"""market_tpsl 分析層: SMAクロス・シグナル (L2 / 段階2.5)。

短期SMAが長期SMAを上抜け→買い、下抜け→売りで成行参入プランを1件出す。
固定SL/TP距離(整数ティック)は発注時に決め、エントリー参考価格は確定足終値。

出力契約は TradingLoop が消費する `ProviderOutput` / `TradePlan`(分析層の
共通I/O語彙。現状は strategies/narrow_focus/signals.py に定義)。Narrow Focus 固有
フィールド(w1_high_int 等)は本手法では 0/空に置き、執行側 MarketTpSlExecution は
limit_price_int(参入参考)・sl_int(固定SL)・fib_target_int(固定TP)・volume_steps
のみを解釈する。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from infers.ai.gateway import JudgementKind, JudgementRequest
from infers.core.models import Candle, Timeframe
from infers.indicators import SMA
from infers.strategies.narrow_focus.signals import ProviderOutput, TradePlan

_FAR_FUTURE = datetime(2999, 1, 1, tzinfo=timezone.utc)


class SmaCrossProvider:
    """SMAクロスで成行プランを出す最小 SignalProvider。"""

    def __init__(self, *, symbol: str, tf: Timeframe,
                 fast: int = 20, slow: int = 50,
                 sl_ticks: int = 300, tp_ticks: int = 600,
                 volume_steps: int = 2) -> None:
        if fast >= slow:
            raise ValueError("fast period must be < slow period")
        if sl_ticks < 1 or tp_ticks < 1:
            raise ValueError("sl_ticks/tp_ticks must be >= 1")
        self._symbol = symbol
        self._tf = tf
        self._fast = SMA(fast)
        self._slow = SMA(slow)
        self._sl_ticks = sl_ticks
        self._tp_ticks = tp_ticks
        self._volume_steps = volume_steps
        self._prev_sign: int | None = None   # 直近の (fast - slow) の符号

    def on_candle(self, candle: Candle) -> ProviderOutput:
        out = ProviderOutput()
        fast = self._fast.update(candle.c_int)
        slow = self._slow.update(candle.c_int)
        if fast is None or slow is None:
            return out  # ウォームアップ中

        diff = fast - slow
        sign = 1 if diff > 0 else (-1 if diff < 0 else 0)
        prev = self._prev_sign
        self._prev_sign = sign

        direction = 0
        if prev is not None and prev <= 0 and sign > 0:
            direction = +1          # ゴールデンクロス → 買い
        elif prev is not None and prev >= 0 and sign < 0:
            direction = -1          # デッドクロス → 売り
        if direction == 0:
            return out

        entry = candle.c_int
        sl = entry - direction * self._sl_ticks
        tp = entry + direction * self._tp_ticks
        plan_id = f"{self._symbol}-{candle.open_time.isoformat()}-{'L' if direction > 0 else 'S'}"
        request = JudgementRequest(
            kind=JudgementKind.ENTRY_GATE, symbol=self._symbol, direction=direction,
            features={"strategy": "sma_cross", "sma_fast": str(fast),
                      "sma_slow": str(slow), "diff": str(diff)})
        out.plans.append(TradePlan(
            plan_id=plan_id, direction=direction, limit_price_int=entry,
            volume_steps=self._volume_steps, add_volume_steps=0, sl_int=sl,
            expiry=_FAR_FUTURE, invalidation_price=sl,
            w1_high_int=0, fib_target_int=tp,
            request=request, cluster_score=Decimal(3), ambiguity=Decimal(0)))
        return out

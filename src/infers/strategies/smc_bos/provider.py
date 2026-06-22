"""smc_bos 分析層: BOS(Break of Structure) + EMA80フィルタ・シグナル
(L2 / 段階S2〜S4 / spec.md §2・§5.1)。

直近の確定スイングを確定足終値がブレイク(BOS)し、かつ EMA80 に対して
順行側にあるときのみ、成行参入プランを1件出す。Narrow Focus のような
多段コンフルエンスは課さない(構造ブレイク+1本のEMAという少パラメータ
設計。spec.md §2)。

出力契約は `SmcOutput`/`TradePlan`(smc_bos/signals.py。`TradePlan` は
narrow_focus/signals.py の共通語彙を market_tpsl と同じく流用)。Narrow Focus
固有フィールド(w1_high_int 等)は中立値で埋める。`SmcOutput.swing_high_int`/
`swing_low_int` は `SmcExecution.on_bar` への管理シグナル(`be_mode=structure`
のSL前進トリガー。spec.md §3.3)として毎確定足の最新値を渡す。

単一ポジション制約(spec.md §2.5)は、本プロバイダが自身の発行したプランの
固定SL/TPを確定足ごとに自己追跡(ミラー)し、ミラー上で建玉中とみなす間は
新規プランを出さない方式で実現する(プラットフォームの SignalProvider は
実際の約定・決済結果をプロバイダへ通知しないため。narrow_focus のクールダウン
カウンタと同種の「自己申告ベースの抑制」であり、AIゲート拒否等で実際には
約定しなかった場合にミラーが実態とズレる既知の限界も同様に持つ)。

ライブ起動時のウォームアップ(歴史データを on_candle へ素通しして指標だけ
育てる経路。発注はしない)も on_candle を直接呼ぶため、ウォームアップ窓内に
本物のBOSシグナルが含まれているとミラーだけ「建玉中」になってしまい、
実際には一度も発注していないのに以後ずっと新規プランがブロックされる
(2026-06 ライブ検証で実際に発生)。`reset_position_mirror()` は呼び出し側
(LiveRunner 等)がウォームアップ完了直後に呼び、ミラーをフラットへ戻すための
専用フック。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal

from infers.ai.gateway import JudgementKind, JudgementRequest
from infers.core.models import Candle, Timeframe
from infers.indicators import ATR, EMA
from infers.strategies.narrow_focus.signals import TradePlan
from infers.strategies.smc_bos.signals import SmcOutput
from infers.strategies.smc_bos.structure import SwingDetector, bos_direction

_FAR_FUTURE = datetime(2999, 1, 1, tzinfo=timezone.utc)


class _PositionMirror:
    """段階S2(be_mode=off)の固定SL/TPを自己追跡する最小ミラー。"""

    __slots__ = ("direction", "sl_int", "tp_int")

    def __init__(self, direction: int, sl_int: int, tp_int: int) -> None:
        self.direction = direction
        self.sl_int = sl_int
        self.tp_int = tp_int

    def touched_exit(self, candle: Candle) -> bool:
        if self.direction > 0:
            return candle.h_int >= self.tp_int or candle.l_int <= self.sl_int
        return candle.l_int <= self.tp_int or candle.h_int >= self.sl_int


class SmcBosProvider:
    """BOS + EMA80 で成行プランを出す SignalProvider(段階S2)。"""

    def __init__(self, *, symbol: str, tf: Timeframe = Timeframe.M30,
                 ema_period: int = 80, swing_lookback: int = 5,
                 atr_period: int = 14,
                 breakout_buffer_atr: Decimal = Decimal("0.3"),
                 breakout_buffer_ticks: int = 5,
                 sl_buffer_ticks: int = 5,
                 atr_sl_mult: Decimal = Decimal("1.5"),
                 min_stop_distance_ticks: int = 5,
                 rr_target: Decimal = Decimal("3.0"),
                 volume_steps: int = 2) -> None:
        if breakout_buffer_ticks < 0 or sl_buffer_ticks < 0:
            raise ValueError("buffer ticks must be >= 0")
        if min_stop_distance_ticks < 1:
            raise ValueError("min_stop_distance_ticks must be >= 1")
        if atr_sl_mult < 0 or breakout_buffer_atr < 0:
            raise ValueError("ATR coefficients must be >= 0")
        if rr_target <= 0:
            raise ValueError("rr_target must be > 0")
        if volume_steps < 1:
            raise ValueError("volume_steps must be >= 1")

        self._symbol = symbol
        self._tf = tf
        self._ema = EMA(ema_period)
        self._atr = ATR(atr_period)
        self._swings = SwingDetector(swing_lookback)
        self._breakout_buffer_atr = breakout_buffer_atr
        self._breakout_buffer_ticks = breakout_buffer_ticks
        self._sl_buffer_ticks = sl_buffer_ticks
        self._atr_sl_mult = atr_sl_mult
        self._min_stop_distance_ticks = min_stop_distance_ticks
        self._rr_target = rr_target
        self._volume_steps = volume_steps

        self._swing_high: int | None = None
        self._swing_low: int | None = None
        self._position: _PositionMirror | None = None

    def reset_position_mirror(self) -> None:
        """建玉ミラーをフラットへ戻す(指標状態は維持)。

        ライブ起動時のウォームアップ(発注しない歴史データ素通し)が
        ミラーを誤って「建玉中」にしてしまうケースの後始末用。呼び出し側
        (LiveRunner.warm_up_provider 等)がウォームアップ完了直後に呼ぶ。
        """
        self._position = None

    def on_candle(self, candle: Candle) -> SmcOutput:
        ema_val = self._ema.update(candle.c_int)
        atr_val = self._atr.update(candle.h_int, candle.l_int, candle.c_int)
        for swing in self._swings.update(candle):
            if swing.kind == "HIGH":
                self._swing_high = swing.price_int
            else:
                self._swing_low = swing.price_int

        # be_mode=structure のSL前進シグナル: どの早期return経路でも最新値を渡す。
        out = SmcOutput(swing_high_int=self._swing_high, swing_low_int=self._swing_low)

        # 単一ポジション制約: ミラー上で建玉中ならまずTP/SL到達を判定し
        # フラットへ戻す(段階S2はSL前進が無いため固定値の到達判定のみ)。
        if self._position is not None and self._position.touched_exit(candle):
            self._position = None

        if self._position is not None or ema_val is None or atr_val is None:
            return out  # 建玉中、またはインジケーター・ウォームアップ中

        buffer_ticks = max(
            int((self._breakout_buffer_atr * atr_val).to_integral_value(
                rounding=ROUND_HALF_EVEN)),
            self._breakout_buffer_ticks)
        direction = bos_direction(candle.c_int, swing_high=self._swing_high,
                                  swing_low=self._swing_low, buffer_ticks=buffer_ticks)
        if direction == 0:
            return out
        if direction > 0 and not candle.c_int > ema_val:
            return out
        if direction < 0 and not candle.c_int < ema_val:
            return out

        entry = candle.c_int
        sl = self._initial_sl(direction, entry, atr_val)
        risk = abs(entry - sl)
        reward = int((self._rr_target * Decimal(risk)).to_integral_value(
            rounding=ROUND_HALF_EVEN))
        tp = entry + direction * reward

        plan_id = f"{self._symbol}-{candle.open_time.isoformat()}-{'L' if direction > 0 else 'S'}"
        request = JudgementRequest(
            kind=JudgementKind.ENTRY_GATE, symbol=self._symbol, direction=direction,
            features={"strategy": "smc_bos", "ema80": str(ema_val), "atr": str(atr_val),
                      "swing_high": str(self._swing_high), "swing_low": str(self._swing_low)})
        out.plans.append(TradePlan(
            plan_id=plan_id, direction=direction, limit_price_int=entry,
            volume_steps=self._volume_steps, add_volume_steps=0, sl_int=sl,
            expiry=_FAR_FUTURE, invalidation_price=sl,
            w1_high_int=0, fib_target_int=tp,
            request=request, cluster_score=Decimal(3), ambiguity=Decimal(0)))
        self._position = _PositionMirror(direction=direction, sl_int=sl, tp_int=tp)
        return out

    def _initial_sl(self, direction: int, entry: int, atr_val: Decimal) -> int:
        """構造SL優先、ATR/最小距離を下限とする(spec.md §3.1)。

        final_dist = max(構造距離, atr_sl_mult×ATR, min_stop_distance)。
        構造側の対抗スイングが未確定の場合は構造距離を0として扱う
        (ATR/最小距離の下限のみで決まる)。
        """
        atr_dist = int((self._atr_sl_mult * atr_val).to_integral_value(
            rounding=ROUND_HALF_EVEN))
        if direction > 0:
            structure_dist = (entry - (self._swing_low - self._sl_buffer_ticks)
                             if self._swing_low is not None else 0)
        else:
            structure_dist = ((self._swing_high + self._sl_buffer_ticks) - entry
                             if self._swing_high is not None else 0)
        final_dist = max(structure_dist, atr_dist, self._min_stop_distance_ticks)
        return entry - direction * final_dist

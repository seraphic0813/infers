"""atr_trend_scalp 分析層: EMA9/21 クロス + EMA21 押し目 + ATR/出来高フィルタ・
上位足(M15)EMA50 バイアス (L2 / spec.md §2・§3)。

執行TF(M5)の確定足ごとに以下4条件がすべて成立し、かつ建玉ミラーがフラット
(+ セッションフィルタ有効時は時間帯内)のとき、成行参入プランを1件出す
(ロング。ショートは対称)。判断はすべて確定足クローズ時のみ(CLAUDE.md 第2条)。

  1. 上位足バイアス : 直近確定 M15 終値 > EMA50(M15)
  2. 短期モメンタム : EMA9 > EMA21(M5・ゴールデンクロス状態)
  3. リトレース     : 直近 retrace_lookback 本以内で安値が EMA21 を下回る/接触し、
                      当足終値が EMA21 を回復(反発)している
  4. ボラ&出来高   : ATR14 >= atr_vol_mult × 直近20本平均ATR
                      かつ volume >= vol_mult × 出来高SMA20

既定パラメータは v1.1 チューニング(spec.md §7・§12)の確定値:
vol_mult=1.6 / tp1_atr_mult=0.5 / tp2_atr_mult=3.0 / trail_atr_mult=0.15。
XAUUSD M5・5年の全期間バックテストで PF1.55・全暦年 PF>1.35・DD$71 を得た構成
(根幹=4条件・50/50分割・建値化+トレールは不変。数値パラメータのみの調整)。
v1.2 ステージ③(spec.md §15)で atr_floor_bps=5.0(相対ボラフロア。ATR/価格比が
低い足を見送る)を追加採用し、PF1.618・全暦年PF>1.30・DD$72.46 へ更新。

出口ジオメトリ(初期SL・TP1・TP2・トレール幅)はエントリー時ATRから確定させ
`AtrTrendPlan` に載せる(spec.md §3・§4)。半玉数はプランに焼き込まず、可変ロット
(`--risk-pct`)対応のため `AtrTrendExecution` が place() 時点の実 volume から導出する。

単一ポジション制約は smc_bos と同じ「自己申告ミラー」で近似する(プラットフォームの
SignalProvider は実際の約定・決済結果を通知しないため)。`_PositionMirror` は
`AtrTrendExecution` の分割決済FSM(TP1到達で建値化しRUNNERへ、以後トレールSL前進、
TP2到達で全決済)をブローカーと結線せず価格のみで独立に再現する(真の建玉管理は
`AtrTrendExecution` が担う。本ミラーは新規プラン抑制の判定材料に過ぎない)。実約定
(スプレッド分のずれ)は無視した概算のため僅かな乖離はあり得るが、初期SL/TP2の固定値
到達のみで判定していた旧実装と異なり、建値化・トレールでSLが前進した後の決済も追跡
する。`reset_position_mirror()` はライブ起動時のウォームアップ(発注しない歴史データ
素通し)後にミラーを戻す汎用フック。
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal

from infers.ai.gateway import JudgementKind, JudgementRequest
from infers.core.models import Candle, Timeframe
from infers.indicators import ATR, EMA, SMA
from infers.strategies.atr_trend_scalp.resample import TfResampler
from infers.strategies.atr_trend_scalp.signals import AtrTrendOutput, AtrTrendPlan

_FAR_FUTURE = datetime(2999, 1, 1, tzinfo=timezone.utc)


class _PositionMirror:
    """建玉中の新規プランを抑制するミラー(`AtrTrendExecution` の分割決済FSMを

    ブローカーと結線せず独立に再現する近似)。

    `AtrTrendExecution.on_bar` と同じ規則(TP1到達で建値化しRUNNERへ、以後は
    トレールSL前進、TP2到達で全決済)を、プロバイダが受け取るのと同一の確定足
    系列に対して価格のみで追跡する。判定順序も `engine.py` と同じく「このバーの
    SL到達判定(前バー終端のSL値)→ TP1/トレール/TP2」に揃える(SL_HITは
    `on_broker_event` が `on_bar` より先に配送されるため、実執行でも同一バー内で
    SLが優先する)。実約定(スプレッド分のずれ)は無視した概算(建値=エントリー
    参考価格のまま)であり、半玉数量は追跡しない(状態遷移そのものは数量に依らない
    ため、フラット判定には不要)。
    """

    __slots__ = ("direction", "entry_int", "sl_int", "tp1_int", "tp2_int",
                 "trail_distance", "runner")

    def __init__(self, *, direction: int, entry_int: int, sl_int: int,
                 tp1_int: int, tp2_int: int, trail_distance: int) -> None:
        self.direction = direction
        self.entry_int = entry_int
        self.sl_int = sl_int
        self.tp1_int = tp1_int
        self.tp2_int = tp2_int
        self.trail_distance = trail_distance
        self.runner = False   # TP1到達後(建値化・トレール中)か

    def _profit_side(self, a: int, b: int) -> int:
        return self.direction * (a - b)

    def _advance_sl(self, target: int) -> None:
        if self._profit_side(target, self.sl_int) > 0:
            self.sl_int = target

    def touched_exit(self, candle: Candle) -> bool:
        """この確定足で建玉が完全にフラットへ戻るなら True(ミラー解除)。"""
        touch = candle.h_int if self.direction > 0 else candle.l_int
        sl_touch = candle.l_int if self.direction > 0 else candle.h_int

        # 1) SL到達(前バー終端のSL値。建値化・トレール後のSLも対象)。
        if self._profit_side(self.sl_int, sl_touch) >= 0:
            return True

        # 2) TP1到達 → 建値化 + RUNNER化(初回のみ)。
        if not self.runner and self._profit_side(touch, self.tp1_int) >= 0:
            self.runner = True
            self._advance_sl(self.entry_int)

        # 3) RUNNER: トレール前進 + TP2到達で全決済。
        if self.runner:
            self._advance_sl(touch - self.direction * self.trail_distance)
            if self._profit_side(touch, self.tp2_int) >= 0:
                return True
        return False


class AtrTrendScalpProvider:
    """EMA クロス + 押し目 + ATR/出来高フィルタで成行プランを出す SignalProvider。"""

    def __init__(self, *, symbol: str, tf: Timeframe = Timeframe.M5,
                 htf: Timeframe = Timeframe.M15,
                 ema_fast_period: int = 9, ema_medium_period: int = 21,
                 ema_trend_period: int = 50,
                 atr_period: int = 14, vol_sma_period: int = 20,
                 atr_avg_period: int = 20, retrace_lookback: int = 3,
                 atr_vol_mult: Decimal = Decimal("1.1"),
                 vol_mult: Decimal = Decimal("1.6"),
                 sl_atr_mult: Decimal = Decimal("1.0"),
                 tp1_atr_mult: Decimal = Decimal("0.5"),
                 tp2_atr_mult: Decimal = Decimal("3.0"),
                 trail_atr_mult: Decimal = Decimal("0.15"),
                 min_stop_distance_ticks: int = 5,
                 volume_steps: int = 2,
                 vol_mult_max: Decimal = Decimal("0"),
                 range_atr_mult_max: Decimal = Decimal("0"),
                 atr_floor_bps: Decimal = Decimal("5.0"),
                 trail_stage2_trigger_atr_mult: Decimal = Decimal("0"),
                 trail_stage2_atr_mult: Decimal = Decimal("0"),
                 session_filter: bool = False,
                 session_start_hour_utc: int = 7,   # JST16:00 = UTC07:00
                 session_end_hour_utc: int = 16,    # JST25:00(翌01:00) = UTC16:00
                 ) -> None:
        if min_stop_distance_ticks < 1:
            raise ValueError("min_stop_distance_ticks must be >= 1")
        if retrace_lookback < 1:
            raise ValueError("retrace_lookback must be >= 1")
        if volume_steps < 1:
            raise ValueError("volume_steps must be >= 1")
        if not (tp2_atr_mult > tp1_atr_mult > 0):
            raise ValueError("require tp2_atr_mult > tp1_atr_mult > 0")
        for m in (atr_vol_mult, vol_mult, sl_atr_mult, trail_atr_mult):
            if m <= 0:
                raise ValueError("multipliers must be > 0")
        if vol_mult_max != 0 and vol_mult_max <= vol_mult:
            raise ValueError("vol_mult_max must be 0 (disabled) or > vol_mult")
        if range_atr_mult_max < 0:
            raise ValueError("range_atr_mult_max must be >= 0")
        if atr_floor_bps < 0:
            raise ValueError("atr_floor_bps must be >= 0")
        if trail_stage2_trigger_atr_mult < 0:
            raise ValueError("trail_stage2_trigger_atr_mult must be >= 0")
        if trail_stage2_trigger_atr_mult != 0:
            if trail_stage2_atr_mult <= 0:
                raise ValueError(
                    "trail_stage2_atr_mult must be > 0 when "
                    "trail_stage2_trigger_atr_mult is set")
            if trail_stage2_atr_mult <= trail_atr_mult:
                raise ValueError(
                    "trail_stage2_atr_mult must be > trail_atr_mult")
        elif trail_stage2_atr_mult != 0:
            raise ValueError(
                "trail_stage2_atr_mult requires trail_stage2_trigger_atr_mult > 0")
        if session_filter and not (0 <= session_start_hour_utc < session_end_hour_utc <= 24):
            raise ValueError("require 0 <= session_start < session_end <= 24 (no wrap)")

        self._symbol = symbol
        self._tf = tf
        self._htf = htf
        self._ema_fast = EMA(ema_fast_period)
        self._ema_medium = EMA(ema_medium_period)
        self._atr = ATR(atr_period)
        self._vol_sma = SMA(vol_sma_period)
        self._atr_avg_period = atr_avg_period
        self._atr_window: deque[Decimal] = deque(maxlen=atr_avg_period)
        self._retrace_lookback = retrace_lookback
        # 各要素は「そのバーの安値が EMA21 以下だったか(ロング押し目)/高値が EMA21
        # 以上だったか(ショート戻り)」の (below, above) タプル。maxlen で直近窓に限定。
        self._retrace: deque[tuple[bool, bool]] = deque(maxlen=retrace_lookback)

        self._htf_rs = TfResampler(symbol, htf)
        self._ema_trend = EMA(ema_trend_period)
        self._htf_close: int | None = None

        self._atr_vol_mult = atr_vol_mult
        self._vol_mult = vol_mult
        self._sl_atr_mult = sl_atr_mult
        self._tp1_atr_mult = tp1_atr_mult
        self._tp2_atr_mult = tp2_atr_mult
        self._trail_atr_mult = trail_atr_mult
        self._min_stop_distance_ticks = min_stop_distance_ticks
        self._volume_steps = volume_steps
        self._vol_mult_max = vol_mult_max
        self._range_atr_mult_max = range_atr_mult_max
        self._atr_floor_bps = atr_floor_bps
        self._trail_stage2_trigger_atr_mult = trail_stage2_trigger_atr_mult
        self._trail_stage2_atr_mult = trail_stage2_atr_mult
        self._session_filter = session_filter
        self._session_start = session_start_hour_utc
        self._session_end = session_end_hour_utc

        self._position: _PositionMirror | None = None

    def reset_position_mirror(self) -> None:
        """建玉ミラーをフラットへ戻す(指標状態は維持)。ウォームアップ後始末用フック。"""
        self._position = None

    # -- ヘルパー ------------------------------------------------------------------

    @staticmethod
    def _round(value: Decimal) -> int:
        return int(value.to_integral_value(rounding=ROUND_HALF_EVEN))

    def _in_session(self, candle: Candle) -> bool:
        if not self._session_filter:
            return True
        return self._session_start <= candle.open_time.hour < self._session_end

    def _update_htf(self, candle: Candle) -> None:
        """M5 を上位足リサンプラへ投入し、確定 M15 で EMA50 と終値を進める。"""
        completed = self._htf_rs.push(candle)
        if completed is not None:
            self._ema_trend.update(completed.c_int)
            self._htf_close = completed.c_int

    # -- SignalProvider 抽象 -------------------------------------------------------

    def on_candle(self, candle: Candle) -> AtrTrendOutput:
        ema_fast = self._ema_fast.update(candle.c_int)
        ema_medium = self._ema_medium.update(candle.c_int)
        atr_val = self._atr.update(candle.h_int, candle.l_int, candle.c_int)
        vol_sma = self._vol_sma.update(candle.volume)
        self._update_htf(candle)
        ema_trend = self._ema_trend.value

        if atr_val is not None:
            self._atr_window.append(atr_val)
        # リトレース窓の更新(EMA21 準備後のみ意味を持つ。準備前は中立 False)。
        if ema_medium is not None:
            below = Decimal(candle.l_int) <= ema_medium
            above = Decimal(candle.h_int) >= ema_medium
        else:
            below = above = False
        self._retrace.append((below, above))

        out = AtrTrendOutput(ema_fast=ema_fast, ema_medium=ema_medium,
                             ema_trend=ema_trend, atr=atr_val, vol_sma=vol_sma)

        # 単一ポジション制約: ミラー上で建玉中なら概算到達判定でフラットへ戻す。
        if self._position is not None and self._position.touched_exit(candle):
            self._position = None
        if self._position is not None:
            return out
        if not self._in_session(candle):
            return out

        # 全指標のウォームアップ完了が前提。
        if (ema_fast is None or ema_medium is None or atr_val is None
                or vol_sma is None or ema_trend is None or self._htf_close is None
                or len(self._atr_window) < self._atr_avg_period):
            return out

        # 条件1(上位足バイアス)+ 条件2(短期モメンタム)で方向を確定。
        if ema_fast > ema_medium and self._htf_close > ema_trend:
            direction = +1
        elif ema_fast < ema_medium and self._htf_close < ema_trend:
            direction = -1
        else:
            return out

        # 条件3(リトレース): 窓内で押し目/戻りタッチ かつ 当足で EMA21 を回復。
        if direction > 0:
            touched = any(b for b, _ in self._retrace)
            rebounded = Decimal(candle.c_int) > ema_medium
        else:
            touched = any(a for _, a in self._retrace)
            rebounded = Decimal(candle.c_int) < ema_medium
        if not (touched and rebounded):
            return out

        # 条件4(ボラ&出来高フィルタ)。
        avg_atr = sum(self._atr_window, Decimal(0)) / self._atr_avg_period
        if atr_val < self._atr_vol_mult * avg_atr:
            return out
        # 相対ボラフロア(v1.2 ステージ③・既定OFF。価格比bpsで判定し年による
        # 価格水準変化の影響を受けない)。
        if (self._atr_floor_bps != 0
                and atr_val * Decimal(10000) < self._atr_floor_bps * Decimal(candle.c_int)):
            return out
        if Decimal(candle.volume) < self._vol_mult * vol_sma:
            return out
        # スパイク足フィルター(v1.2 ステージ①・既定OFF)。
        if self._vol_mult_max != 0 and Decimal(candle.volume) > self._vol_mult_max * vol_sma:
            return out
        if (self._range_atr_mult_max != 0
                and Decimal(candle.h_int - candle.l_int) > self._range_atr_mult_max * atr_val):
            return out

        # 出口ジオメトリ(エントリー時ATR固定)。
        atr_ticks = self._round(atr_val)
        tp1_dist = self._round(self._tp1_atr_mult * atr_val)
        tp2_dist = self._round(self._tp2_atr_mult * atr_val)
        trail = max(1, self._round(self._trail_atr_mult * atr_val))
        sl_dist = max(self._round(self._sl_atr_mult * atr_val),
                      self._min_stop_distance_ticks)
        # v1.2 ステージ⑤-A: 段階式トレール(既定OFF。エントリー時ATR固定)。
        stage2_trigger = (self._round(self._trail_stage2_trigger_atr_mult * atr_val)
                          if self._trail_stage2_trigger_atr_mult != 0 else 0)
        stage2_distance = (max(1, self._round(self._trail_stage2_atr_mult * atr_val))
                           if self._trail_stage2_trigger_atr_mult != 0 else 0)
        # ATR が極小でTP1/TP2距離が縮退する足は見送り(有効な分割決済を作れない)。
        if atr_ticks < 1 or not (tp2_dist > tp1_dist >= 1):
            return out

        entry = candle.c_int
        sl = entry - direction * sl_dist
        tp1 = entry + direction * tp1_dist
        tp2 = entry + direction * tp2_dist

        plan_id = (f"{self._symbol}-{candle.open_time.isoformat()}-"
                   f"{'L' if direction > 0 else 'S'}")
        request = JudgementRequest(
            kind=JudgementKind.ENTRY_GATE, symbol=self._symbol, direction=direction,
            features={"strategy": "atr_trend_scalp",
                      "ema_fast": str(ema_fast), "ema_medium": str(ema_medium),
                      "ema_trend": str(ema_trend), "atr": str(atr_val),
                      "vol_sma": str(vol_sma), "volume": str(candle.volume)})
        out.plans.append(AtrTrendPlan(
            plan_id=plan_id, direction=direction, limit_price_int=entry,
            volume_steps=self._volume_steps, sl_int=sl, expiry=_FAR_FUTURE,
            invalidation_price=sl, fib_target_int=tp2, tp1_int=tp1,
            trail_distance_ticks=trail, atr_at_entry_int=atr_ticks,
            request=request, cluster_score=Decimal(3), ambiguity=Decimal(0),
            trail_stage2_trigger_int=stage2_trigger,
            trail_stage2_distance_ticks=stage2_distance))
        self._position = _PositionMirror(
            direction=direction, entry_int=entry, sl_int=sl,
            tp1_int=tp1, tp2_int=tp2, trail_distance=trail)
        return out

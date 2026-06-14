"""INFERS 戦略プロバイダ — 分析層パイプラインの結合 (フェーズ9 / 設計書 §1.1)。

確定足1本ごとに以下を自律実行し、TradingLoop へ引き渡す:

  インジケーター更新 (SMA/ATR/RSI)
    → ZigZag (ATR連動の動的閾値・確定遅延)
      → ダウFSM (HH/HL/LH/LL → StructureEvent: 建値SL移動のトリガー源)
      → エリオット計数 (候補集合 + 無効化価格 + 曖昧度)
        → 未来裁量エンジン (RSIバンド × SMA前方投影 × レジサポ → (k,P)マップ)
          → TradePlan (expiry・無効化価格つき指値候補)

打診プランの組成規則 (マニュアル準拠):
  - 方向はダウ理論の確定トレンドのみ (UP→買い / DOWN→売り。SUSPECT/未確定は見送り)
  - 対象は「第2波進行中」のカウント候補 (押し目を未来裁量で先回りする)
  - 無効化価格 = エリオット原則② (P0)。SLはその外側 (sl_buffer_atr)
  - 追撃基準 w1_high = P1、半分利確 = 指値 + 第1波長 × 1.618
  - クールダウン: プラン発行後 N 本は再発行しない (乱射防止。
    同一plan_idの冪等性は TradingLoop 側でも担保される)
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal

from infers.ai.gateway import JudgementKind, JudgementRequest
from infers.analysis.dow import DowStateMachine, StructureEvent, TrendState
from infers.analysis.elliot import ElliottCounter, ElliottView, WaveCount
from infers.analysis.future_discretion import build_future_map, propose_limit_orders
from infers.analysis.indicators import ATR, SMA, WilderRSI
from infers.analysis.micro import (
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    RSI_REVERSAL_LOOKBACK,
    RsiExtremeRecency,
)
from infers.analysis.support_resistance import build_zones
from infers.analysis.zigzag import SwingPoint, ZigZagDetector
from infers.core.loop import ProviderOutput, TradePlan
from infers.data.models import Candle, Timeframe

FIB_161_8 = Decimal("1.618")

# 第2波押し目のFIBリトレースメント比率 (マニュアル 2.3 / 設計書 §5.4 FIBファミリー)
FIB_RETRACE_RATIOS: tuple[Decimal, ...] = (
    Decimal("0.382"), Decimal("0.5"), Decimal("0.618"), Decimal("0.786"))


def macro_gate(micro_dir: int | None, macro_state: TrendState) -> int | None:
    """マクロトレンド方向と一致するミクロエントリー方向のみを通す (設計書 §1 フラクタル)。

    マクロが確定 UP のとき買いのみ、確定 DOWN のとき売りのみ許可。マクロが
    未確定 (UNDEFINED) / 転換警戒 (SUSPECT) または逆行なら見送り (NO-TRADE 既定)。
    """
    if micro_dir is None:
        return None
    if macro_state is TrendState.UP and micro_dir > 0:
        return micro_dir
    if macro_state is TrendState.DOWN and micro_dir < 0:
        return micro_dir
    return None


class MacroResampler:
    """M5確定足を上位足 (macro_tf) に集約し、完成した上位足を1本ずつ返す。

    設計書 §1「単一の真実は最小足。上位足はリサンプラが生成」に従う。境界は
    UTC時刻の床関数 (H4 → 0/4/8/12/16/20時、D1 → 00:00)。ブローカー日次境界の
    厳密補正は将来課題。確定したバーのみ返す (形成中バーは出さない: 第2条)。
    """

    def __init__(self, symbol: str, macro_tf: Timeframe) -> None:
        self.symbol = symbol
        self.tf = macro_tf
        self._dur = int(macro_tf.duration.total_seconds())
        self._bucket: int | None = None
        self._start: datetime | None = None
        self._o = self._h = self._l = self._c = 0

    def push(self, candle: Candle) -> Candle | None:
        """M5足を1本投入。直前の上位足が確定したらそれを返す (なければ None)。"""
        b = (int(candle.open_time.timestamp()) // self._dur) * self._dur
        completed: Candle | None = None
        if self._bucket is None:
            self._open(b, candle)
        elif b != self._bucket:
            completed = self._emit()
            self._open(b, candle)
        else:
            if candle.h_int > self._h:
                self._h = candle.h_int
            if candle.l_int < self._l:
                self._l = candle.l_int
            self._c = candle.c_int
        return completed

    def _open(self, b: int, candle: Candle) -> None:
        self._bucket = b
        self._start = datetime.fromtimestamp(b, tz=timezone.utc)
        self._o, self._h, self._l, self._c = (
            candle.o_int, candle.h_int, candle.l_int, candle.c_int)

    def _emit(self) -> Candle:
        assert self._start is not None
        return Candle(symbol=self.symbol, tf=self.tf, open_time=self._start,
                      o_int=self._o, h_int=self._h, l_int=self._l, c_int=self._c,
                      volume=0, is_closed=True)


def fib_retrace_levels(p0_int: int, p1_int: int,
                       ratios: tuple[Decimal, ...] = FIB_RETRACE_RATIOS) -> list[int]:
    """第1波 (P0→P1) の押し戻し水準価格 (整数ティック)。

    未来裁量マップの FIB ファミリー入力。方向は P0/P1 の大小関係に内包される
    (上昇波なら P1 から下へ、下降波なら上へ戻る)。
    """
    levels: list[int] = []
    for r in ratios:
        offset = (Decimal(p1_int - p0_int) * r).to_integral_value(
            rounding=ROUND_HALF_EVEN)
        levels.append(p1_int - int(offset))
    return levels


@dataclass(frozen=True)
class ProviderConfig:
    """戦略パラメータ (config/thresholds.yaml から注入。要バックテスト調整)。"""

    k_atr_reversal: Decimal = Decimal("1.5")    # ZigZag反転閾値 = k × ATR
    sma_periods: tuple[int, ...] = (90, 200)
    rsi_period: int = 14
    rsi_macro_tfs: tuple[Timeframe, ...] = (Timeframe.H1, Timeframe.D1)  # マルチTF RSI (G2-⑤)
    # 上位足RSIトリガーの反発/反落許容窓 (G2-⑤「到達、またはそこから反発/反落」)。
    # 0 で「現在圏内のみ」、>0 で直近この本数以内に圏内到達があれば有効。
    rsi_reversal_lookback: int = RSI_REVERSAL_LOOKBACK
    atr_period: int = 14
    grid_k_max: int = 12                         # 未来裁量の時間方向ホライズン (バー)
    price_step_atr: Decimal = Decimal("0.1")     # 価格グリッド刻み = 0.1 × ATR
    sma_tol_atr: Decimal = Decimal("0.3")        # SMA接触許容幅
    fib_tol_atr: Decimal = Decimal("0.3")        # FIBリトレース水準の許容幅
    fib_ratios: tuple[Decimal, ...] = FIB_RETRACE_RATIOS
    sl_buffer_atr: Decimal = Decimal("0.5")      # SL = 無効化価格 − buffer
    probe_volume_steps: int = 2
    add_volume_steps: int = 2
    cooldown_bars: int = 12
    require_core_family: bool = True             # 中核根拠(SMA/RSI)欠如プランをL0で除外 (P6)
    score_fib: bool = True                       # FIBをコンフルエンス・スコアに含めるか (検証用に除外可)
    tp_sr_min_touches: int = 2                   # 半分利確の「重要SRゾーン」最小タッチ数 (§6.4)
    tp_sr_min_strength: Decimal = Decimal("1.5")  # 同・最小減衰加重強度
    # マクロ方向フィルター (設計書 §1 フラクタル: 上位足トレンドと一致時のみ発注)
    macro_filter: bool = True                    # 有効化 (False で従来のミクロ単独方向)
    macro_tf: Timeframe = Timeframe.D1           # 方向を見定めるマクロ足 (手法ゲート1: D1/H1)
    # 上位足エリオット第2波判定 (entry-methodology.md: 本物の第2波を上位足で数える)
    macro_wave2: bool = False                    # True で wave2_tf のエリオットを wave-2 選択に使う
    wave2_tf: Timeframe = Timeframe.H4           # 第2波(押し目)カウント用TF。方向TFと独立
    #   ↑ 方向はD1(大局)・押し目はやや細かい足(H4)で捉える。両者を同一TFにすると、
    #     方向TFで押し目を待つ=そのTFの転換に見え、買いが消える論理矛盾が起きる。
    # 建値SL移動の構造トリガーTF。既定M5(高速)。True で上位足だが、半分利確が
    # 建値SL移動に依存する現結合では上位足はパイプラインを止めるため要注意。
    be_sl_macro_tf: bool = False                 # True で建値SLを上位足ダウ構造で動かす
    # 40%深さスクリーニング (entry-methodology.md G2-⑥): 第1波の深い押し目/戻りのみ。
    # 本物の第2波 (macro_wave2) が前提。M5ノイズの偽第2波では浅い押し目が勝つため既定OFF。
    depth_screen: bool = False                   # True で深い押し目に限定
    depth_max: Decimal = Decimal("0.40")         # 直近スイング幅に対する押し目の最大位置 (下方40%)
    max_swings: int = 24                         # レジサポ用スイングバッファ
    max_grid_points: int = 120
    closes_window: int = 400                     # SMA前方投影用の終値履歴


class InfersSignalProvider:
    """単一 (symbol, tf) 系列の Narrow Focus パイプライン。"""

    def __init__(self, *, symbol: str, tf: Timeframe,
                 config: ProviderConfig | None = None) -> None:
        self.symbol = symbol
        self.tf = tf
        self.cfg = config or ProviderConfig()

        self._smas = {p: SMA(p) for p in self.cfg.sma_periods}
        self._atr = ATR(self.cfg.atr_period)
        self._rsi = WilderRSI(self.cfg.rsi_period)
        self._zz = ZigZagDetector(reversal_ticks=1)      # 閾値は毎バー上書き
        self._dow = DowStateMachine()
        self._elliott = ElliottCounter()
        self._swings: deque[SwingPoint] = deque(maxlen=self.cfg.max_swings)
        self._closes: deque[int] = deque(maxlen=self.cfg.closes_window)
        self._view: ElliottView | None = None
        self._cooldown = 0

        # マクロ方向フィルター (方向TF=macro_tf をリサンプル → 独立ダウFSM)。
        # 手法ゲート1: D1/H1 の大局トレンドで方向を確定し、押し目で揺れないようにする。
        self._macro_rs = MacroResampler(symbol, self.cfg.macro_tf)
        self._macro_atr = ATR(self.cfg.atr_period)
        self._macro_zz = ZigZagDetector(reversal_ticks=1)
        self._macro_dow = DowStateMachine()
        # 第2波カウント (押し目TF=wave2_tf を独立リサンプル → エリオット計数)。
        # 方向TFと分離: 方向は大局(D1)、押し目はやや細かい足(H4)で本物の第2波を数える。
        self._w2_rs = MacroResampler(symbol, self.cfg.wave2_tf)
        self._w2_atr = ATR(self.cfg.atr_period)
        self._w2_zz = ZigZagDetector(reversal_ticks=1)
        self._w2_elliott = ElliottCounter()
        self._w2_view: ElliottView | None = None
        # マルチTF RSI (手法G2-⑤): 上位足(H1/D1)を独立リサンプル → WilderRSI。
        # 各足について「極値圏に到達、またはそこから反発/反落した直後」を
        # RsiExtremeRecency で判定し、コンフルエンスの加点に使う(重なるほど強い)。
        self._rsi_mtf: list[tuple[MacroResampler, WilderRSI, RsiExtremeRecency]] = [
            (MacroResampler(symbol, rtf), WilderRSI(self.cfg.rsi_period),
             RsiExtremeRecency(self.cfg.rsi_reversal_lookback))
            for rtf in self.cfg.rsi_macro_tfs
        ]

    # -- パイプライン本体 ----------------------------------------------------------

    def on_candle(self, candle: Candle) -> ProviderOutput:
        if not candle.is_closed:
            raise ValueError("provider accepts closed candles only (CLAUDE.md rule 2)")
        if candle.symbol != self.symbol or candle.tf is not self.tf:
            raise ValueError(f"series mismatch: expected ({self.symbol},{self.tf})")

        # 1) インジケーター更新
        self._closes.append(candle.c_int)
        for sma in self._smas.values():
            sma.update(candle.c_int)
        self._atr.update(candle.h_int, candle.l_int, candle.c_int)
        self._rsi.update(candle.c_int)
        self._update_rsi_mtf(candle)             # 上位足RSI (H1/D1) を確定時に進める

        output = ProviderOutput()
        # 保有玉の半分利確トリガー (③-1) は毎足の現在RSI/90SMAを使う
        output.rsi_value = self._rsi.value
        sma90 = self._smas.get(90)
        if sma90 is not None and sma90.is_ready:
            output.sma90_int = int(sma90.value.to_integral_value(rounding=ROUND_HALF_EVEN))
        macro_ev = self._update_macro(candle)   # 上位足ダウの構造イベント (確定時)

        # 2) スイング検出 → ダウFSM → エリオット計数 (ATRウォームアップ後)
        if self._atr.is_ready:
            atr = self._atr.value
            assert atr is not None
            theta = max(1, int((self.cfg.k_atr_reversal * atr)
                               .to_integral_value(rounding=ROUND_HALF_EVEN)))
            swing = self._zz.update(candle, reversal_ticks=theta)
            if swing is not None:
                self._swings.append(swing)
                ev = self._dow.on_swing(swing)
                # 建値SLの構造トリガー: be_sl_macro_tf 時は上位足、それ以外はM5 (§②)
                if ev is not None and not self.cfg.be_sl_macro_tf:
                    output.structure_events.append(ev)
                self._view = self._elliott.on_swing(swing)
            # 半分利確の「重要SRゾーン」(§6.4): 複数タッチ & 十分な強度のゾーンのみ
            output.tp_sr_zones = self._important_sr_zones(atr, candle.c_int)

        # be_sl_macro_tf のときは上位足のダウ構造で建値SLを動かす (entry-methodology.md ②)
        if self.cfg.be_sl_macro_tf and macro_ev is not None:
            output.structure_events.append(macro_ev)

        # 3) 未来裁量によるプラン組成 (クールダウン制御つき)
        if self._cooldown > 0:
            self._cooldown -= 1
        elif self._ready():
            output.plans = self._build_plans(candle)
            if output.plans:
                self._cooldown = self.cfg.cooldown_bars
        return output

    def _ready(self) -> bool:
        return (self._atr.is_ready and self._rsi.state is not None
                and self._wave_view() is not None)

    def _important_sr_zones(self, atr: Decimal, ref_price_int: int) -> tuple:
        """半分利確トリガー用の「重要」SRゾーン (設計書 §6.4)。

        単一スイング由来の弱いゾーンを除外し、複数回タッチされ十分な減衰加重
        強度を持つ水準のみを残す (= 実際に意識されている水準)。
        """
        zones = build_zones(list(self._swings), atr, ref_price_int=ref_price_int)
        return tuple(z for z in zones
                     if z.touches >= self.cfg.tp_sr_min_touches
                     and z.strength >= self.cfg.tp_sr_min_strength)

    # -- プラン組成 -----------------------------------------------------------------

    def _update_macro(self, candle: Candle) -> StructureEvent | None:
        """方向TF(macro_tf)と押し目TF(wave2_tf)の上位足を独立に更新する。

        方向TFのダウ構造イベント (HH/HL/LH/LL) を返す。be_sl_macro_tf 時はこれを
        建値SL移動のトリガーに使う (M5ノイズ狩りを避ける: entry-methodology.md ②)。
        """
        self._update_wave2(candle)
        macro_bar = self._macro_rs.push(candle)
        if macro_bar is None:
            return None
        self._macro_atr.update(macro_bar.h_int, macro_bar.l_int, macro_bar.c_int)
        if not self._macro_atr.is_ready:
            return None
        m_atr = self._macro_atr.value
        assert m_atr is not None
        m_theta = max(1, int((self.cfg.k_atr_reversal * m_atr)
                             .to_integral_value(rounding=ROUND_HALF_EVEN)))
        m_swing = self._macro_zz.update(macro_bar, reversal_ticks=m_theta)
        if m_swing is None:
            return None
        return self._macro_dow.on_swing(m_swing)

    def _update_wave2(self, candle: Candle) -> None:
        """押し目TF(wave2_tf)の上位足を更新し、本物の第2波ビュー(_w2_view)を進める。"""
        bar = self._w2_rs.push(candle)
        if bar is None:
            return
        self._w2_atr.update(bar.h_int, bar.l_int, bar.c_int)
        if not self._w2_atr.is_ready:
            return
        w_atr = self._w2_atr.value
        assert w_atr is not None
        w_theta = max(1, int((self.cfg.k_atr_reversal * w_atr)
                             .to_integral_value(rounding=ROUND_HALF_EVEN)))
        swing = self._w2_zz.update(bar, reversal_ticks=w_theta)
        if swing is None:
            return
        self._w2_view = self._w2_elliott.on_swing(swing)

    def _update_rsi_mtf(self, candle: Candle) -> None:
        """上位足(H1/D1)RSIを、各上位足の確定時に進める (マルチTF RSI: G2-⑤)。"""
        for rs, rsi, recency in self._rsi_mtf:
            bar = rs.push(candle)
            if bar is not None:
                rsi.update(bar.c_int)
                v = rsi.value
                if v is not None:
                    recency.update(v)

    def _htf_rsi_extreme(self, direction: int) -> int:
        """上位足RSIが方向にトリガーした TF 数 (買い: ≤30 / 売り: ≥70)。

        手法G2-⑤に従い「今まさに極値圏」だけでなく「直近 lookback 本以内に
        極値圏へ到達 → 反発/反落した直後」も 1 TF としてカウントする。
        """
        return sum(
            1 for _, _, recency in self._rsi_mtf if recency.active(direction)
        )

    def _direction(self) -> int | None:
        """エントリー方向。ミクロ確定トレンドを、マクロ方向フィルターで絞る。

        ミクロ (M5) のダウが UP→買い/DOWN→売り。さらに macro_filter 有効時は
        マクロ足のダウ方向と一致するものだけ通す (設計書 §1 フラクタル)。
        """
        if self._dow.state is TrendState.UP:
            micro: int | None = +1
        elif self._dow.state is TrendState.DOWN:
            micro = -1
        else:
            return None
        if self.cfg.macro_filter:
            return macro_gate(micro, self._macro_dow.state)
        return micro

    def _wave_view(self) -> ElliottView | None:
        """wave-2 判定に使うエリオットビュー。macro_wave2 で上位足の本物の波を使う。"""
        return self._w2_view if self.cfg.macro_wave2 else self._view

    def _select_wave2(self, direction: int, close_int: int) -> WaveCount | None:
        view = self._wave_view()
        if view is None:
            return None
        for wc in view.candidates:
            if (wc.direction == direction and wc.current_wave == 2
                    and not wc.is_invalidated(close_int)):
                return wc
        return None

    def _build_plans(self, candle: Candle) -> list[TradePlan]:
        direction = self._direction()
        if direction is None:
            return []
        wc = self._select_wave2(direction, candle.c_int)
        if wc is None:
            return []

        view = self._wave_view()
        atr = self._atr.value
        assert atr is not None and view is not None
        rsi_state = self._rsi.state
        assert rsi_state is not None

        inv = wc.invalidation_price
        step = max(1, int((self.cfg.price_step_atr * atr)
                          .to_integral_value(rounding=ROUND_HALF_EVEN)))

        # 価格グリッド: 無効化価格の内側 〜 現在値の手前 (買い。売りは対称)
        if direction > 0:
            lo, hi = inv + 1, candle.c_int - step
        else:
            lo, hi = candle.c_int + step, inv - 1
        if hi < lo:
            return []
        span = hi - lo
        if span // step + 1 > self.cfg.max_grid_points:
            step = span // self.cfg.max_grid_points + 1
        prices = list(range(lo, hi + 1, step))

        # 40%深さスクリーニング (entry-methodology.md G2-⑥): 直近スイング(第1波)の
        # 深い押し目/戻りに限定。買いは安値に近い下方40%、売りは高値に近い上方40%。
        # 直近安値のすぐ外側にSLを置けるため R/R を最大化する引き付けルール。
        if self.cfg.depth_screen:
            sw_low = min(wc.pivots[0].price_int, wc.pivots[1].price_int)
            sw_high = max(wc.pivots[0].price_int, wc.pivots[1].price_int)
            sw_span = sw_high - sw_low
            if sw_span > 0:
                margin = int((self.cfg.depth_max * Decimal(sw_span))
                             .to_integral_value(rounding=ROUND_HALF_EVEN))
                if direction > 0:
                    prices = [p for p in prices if p <= sw_low + margin]
                else:
                    prices = [p for p in prices if p >= sw_high - margin]
            if not prices:
                return []

        sr_zones = build_zones(list(self._swings), atr, ref_price_int=candle.c_int)
        fib_levels = fib_retrace_levels(
            wc.pivots[0].price_int, wc.pivots[1].price_int, self.cfg.fib_ratios)
        cells = build_future_map(
            closes=list(self._closes),
            rsi_state=rsi_state,
            direction=direction,
            k_range=range(1, self.cfg.grid_k_max + 1),
            prices=prices,
            sma_periods=self.cfg.sma_periods,
            sr_zones=sr_zones,
            fib_levels=fib_levels,
            sma_tol_ticks=max(1, int((self.cfg.sma_tol_atr * atr)
                                     .to_integral_value(rounding=ROUND_HALF_EVEN))),
            fib_tol_ticks=max(1, int((self.cfg.fib_tol_atr * atr)
                                     .to_integral_value(rounding=ROUND_HALF_EVEN))),
            score_fib=self.cfg.score_fib,
            htf_rsi_extreme=self._htf_rsi_extreme(direction),
        )
        if not cells:
            return []
        candidates = propose_limit_orders(
            cells, direction=direction, now=candle.close_time,
            bar_duration=self.tf.duration, price_step=step,
            invalidation_price=inv)
        if self.cfg.require_core_family:
            # 中核根拠 (SMA/RSI) を欠く SR,FIB のみの候補は手法上ほぼ自動 NO_GO
            # (Phase A: 該当の94%がNO_GO)。L0で除外し判定コストを浪費しない (P6)。
            candidates = [c for c in candidates
                          if "SMA" in c.families or "RSI" in c.families]
            if not candidates:
                return []
        best = candidates[0]

        sl_buffer = max(1, int((self.cfg.sl_buffer_atr * atr)
                               .to_integral_value(rounding=ROUND_HALF_EVEN)))
        sl_int = inv - direction * sl_buffer
        len1 = wc.wave_len(1)
        fib_target = best.limit_price_int + direction * int(
            (Decimal(len1) * FIB_161_8).to_integral_value(rounding=ROUND_HALF_EVEN))
        w1_high = wc.pivots[1].price_int
        w1_low = wc.pivots[0].price_int

        # エントリー根拠の可視化用: limit 近傍 (±1ATR or 内包) の重要SRゾーンを記録
        near_band = max(1, int(atr.to_integral_value(rounding=ROUND_HALF_EVEN)))
        basis_sr = tuple(
            (z.low_int, z.high_int) for z in sr_zones
            if z.contains(best.limit_price_int)
            or min(abs(z.low_int - best.limit_price_int),
                   abs(z.high_int - best.limit_price_int)) <= near_band)

        # 特徴量はすべて文字列化 (JSON決定論化 → VerdictCacheキーの安定性: 第15条)
        features = {
            "dow_state": self._dow.state.name,
            "macro_dow": self._macro_dow.state.name,
            "macro_tf": self.cfg.macro_tf.value,
            "wave2_tf": self.cfg.wave2_tf.value if self.cfg.macro_wave2 else self.tf.value,
            "current_wave": "2",
            "ambiguity": str(view.ambiguity),
            "cluster_score": str(best.score),
            "families": ",".join(best.families),
            "limit": str(best.limit_price_int),
            "invalidation": str(inv),
            "w1_high": str(w1_high),
            "rsi": str(self._rsi.value),
            "rsi_mtf": ",".join(
                f"{rs.tf.value}:{rsi.value if rsi.value is not None else 'NA'}"
                for rs, rsi, _ in self._rsi_mtf),
            "rsi_mtf_extreme": str(self._htf_rsi_extreme(direction)),
            "rsi_band": f"{best.rsi_band[0]}..{best.rsi_band[1]}",
            "eta_bars": f"{best.eta_window[0]}-{best.eta_window[1]}",
            "atr": str(atr),
        }
        plan = TradePlan(
            plan_id=f"{self.symbol}/{self.tf.value}/{candle.open_time.isoformat()}",
            direction=direction,
            limit_price_int=best.limit_price_int,
            volume_steps=self.cfg.probe_volume_steps,
            add_volume_steps=self.cfg.add_volume_steps,
            sl_int=sl_int,
            expiry=best.expiry,
            invalidation_price=inv,
            w1_high_int=w1_high,
            fib_target_int=fib_target,
            request=JudgementRequest(kind=JudgementKind.FUTURE_CONFLUENCE_REVIEW,
                                     symbol=self.symbol, direction=direction,
                                     features=features),
            cluster_score=best.score,
            ambiguity=view.ambiguity,
            w1_low_int=w1_low,
            fib_levels=tuple(fib_levels),
            sr_zones=basis_sr,
        )
        return [plan]

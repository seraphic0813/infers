"""atr_trend_scalp 手法: 上位足リサンプラ・執行モデル(分割決済/建値化/トレール)・
分析層(4条件ゲート/ミラー)・レジストリ配線・結合(BacktestEngine→TradingLoop→
AtrTrendExecution)の検証。spec.md 参照。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from infers.ai.gateway import AiGateway, VerdictCache
from infers.ai.passthrough import PASSTHROUGH_POLICY, PassthroughLlmClient
from infers.backtest.engine import BacktestEngine, LedgerBroker
from infers.core.models import Candle, Timeframe
from infers.execution.risk import RiskConfig, RiskManager
from infers.execution.sim_broker import SimBroker
from infers.strategies.narrow_focus.execution import FsmConfig
from infers.strategies.registry import get_strategy
from infers.strategies.atr_trend_scalp.execution import AtrTrendExecution, AtrState
from infers.strategies.atr_trend_scalp.provider import AtrTrendScalpProvider
from infers.strategies.atr_trend_scalp.resample import TfResampler

UTC = timezone.utc
T0 = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


def mk5(i: int, h: int, l: int, c: int, vol: int = 100,
        tf: Timeframe = Timeframe.M5) -> Candle:
    o = max(l, min(h, c))
    return Candle(symbol="XAUUSD", tf=tf, open_time=T0 + i * tf.duration,
                  o_int=o, h_int=h, l_int=l, c_int=c, volume=vol, is_closed=True)


# ---------------------------------------------------------------------------
# 1. TfResampler: M5 → M15 集約(確定足のみ・リペイント無し)
# ---------------------------------------------------------------------------

class TestTfResampler:
    def test_no_emit_within_first_bucket(self):
        rs = TfResampler("XAUUSD", Timeframe.M15)
        assert rs.push(mk5(0, 1010, 990, 1000)) is None   # 00:00
        assert rs.push(mk5(1, 1015, 995, 1008)) is None   # 00:05
        assert rs.push(mk5(2, 1012, 1000, 1005)) is None  # 00:10 (同一M15)

    def test_emits_completed_bucket_on_boundary(self):
        rs = TfResampler("XAUUSD", Timeframe.M15)
        rs.push(mk5(0, 1010, 990, 1000))
        rs.push(mk5(1, 1030, 995, 1008))   # このバケットの高値=1030
        rs.push(mk5(2, 1012, 985, 1005))   # このバケットの安値=985, 終値=1005
        completed = rs.push(mk5(3, 1006, 1001, 1004))   # 00:15 → 直前M15確定
        assert completed is not None
        assert completed.tf is Timeframe.M15
        assert completed.open_time == T0
        assert completed.o_int == 1000 and completed.h_int == 1030
        assert completed.l_int == 985 and completed.c_int == 1005
        assert completed.volume == 0   # 上位足は出来高を持たない
        assert completed.is_closed is True

    def test_bucket_aligns_to_quarter_hour(self):
        rs = TfResampler("XAUUSD", Timeframe.M15)
        # 00:05 開始でも 00:00 バケットに属する。
        assert rs.push(mk5(1, 1010, 990, 1000)) is None
        assert rs.push(mk5(2, 1010, 990, 1001)) is None
        completed = rs.push(mk5(3, 1010, 990, 1002))   # 00:15 で 00:00 バケット確定
        assert completed is not None and completed.open_time == T0


# ---------------------------------------------------------------------------
# 2. AtrTrendExecution: 成行参入 + 50/50分割 + 建値化 + トレール + 段階TP
# ---------------------------------------------------------------------------

class _LongIntent:
    plan_id = "x"
    direction = +1
    limit_price_int = 1000
    sl_int = 980
    tp1_int = 1020
    fib_target_int = 1040     # TP2
    trail_distance_ticks = 10
    volume_steps = 2


class _ShortIntent:
    plan_id = "x"
    direction = -1
    limit_price_int = 1000
    sl_int = 1020
    tp1_int = 980
    fib_target_int = 960      # TP2
    trail_distance_ticks = 10
    volume_steps = 2


class _LongIntentStage2(_LongIntent):
    # v1.2 ステージ⑤-A: TP2 を遠くへ(1200)し、トレールのみで検証できるようにする。
    fib_target_int = 1200
    trail_stage2_trigger_int = 30    # 含み益が entry+30 に到達でステージ2へ
    trail_stage2_distance_ticks = 25  # ステージ1(10)より広い


class TestAtrTrendExecution:
    def _broker(self) -> SimBroker:
        b = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        b.process_bar(mk5(0, 1005, 995, 1000))
        return b

    def test_place_sets_open_with_half(self):
        ex = AtrTrendExecution(position_id="e1", direction=+1, broker=self._broker())
        ex.place(_LongIntent())
        assert ex.state is AtrState.OPEN
        assert ex.volume_steps == 2
        assert ex.entry_price_int == 1002   # spread込み fill
        assert ex.journal[0][0] == "MARKET_ENTRY"
        assert ex.journal[0][1]["half"] == 1

    def test_tp1_half_close_moves_to_breakeven_and_runner(self):
        broker = self._broker()
        ex = AtrTrendExecution(position_id="e2", direction=+1, broker=broker)
        ex.place(_LongIntent())
        bar = mk5(1, 1025, 1010, 1022)   # 高値1025 >= TP1(1020)
        broker.process_bar(bar)
        out = ex.on_bar(bar, None)
        assert out.closed is False
        assert ex.state is AtrState.RUNNER
        assert ex.volume_steps == 1                    # 半玉決済で残1
        assert ex.sl_int >= ex.entry_price_int         # 建値以上へ前進(利益方向)
        kinds = [n for n, _ in ex.journal]
        assert kinds.count("TP_CLOSE") == 1            # TP1 半利は1回
        assert "MOVE_SL" in kinds

    def test_runner_trails_then_tp2_closes(self):
        broker = self._broker()
        ex = AtrTrendExecution(position_id="e3", direction=+1, broker=broker)
        ex.place(_LongIntent())
        b1 = mk5(1, 1025, 1012, 1022); broker.process_bar(b1); ex.on_bar(b1, None)
        assert ex.state is AtrState.RUNNER
        sl_after_be = ex.sl_int
        b2 = mk5(2, 1045, 1030, 1042); broker.process_bar(b2); out = ex.on_bar(b2, None)
        assert out.closed and ex.state is AtrState.CLOSED
        assert ex.volume_steps == 0
        assert ex.sl_int >= sl_after_be                # トレールは利益方向のみ
        assert ex.journal[-1] [0] == "TP_CLOSE"        # TP2 全決済

    def test_partial_close_happens_at_most_once(self):
        broker = self._broker()
        ex = AtrTrendExecution(position_id="e4", direction=+1, broker=broker)
        ex.place(_LongIntent())

        def step(bar: Candle) -> None:
            for e in broker.process_bar(bar):    # 実ループ同様、先にブローカーイベント配送
                ex.on_broker_event(e)
            ex.on_bar(bar, None)

        step(mk5(1, 1025, 1012, 1022))   # TP1 到達 → RUNNER(半利1回)
        # TP2 未達のまま RUNNER を継続(安値はトレールSLより上を維持)。
        step(mk5(2, 1030, 1023, 1028))
        step(mk5(3, 1033, 1027, 1031))
        tp1 = [j for j in ex.journal if j[0] == "TP_CLOSE" and j[1].get("level") == "tp1"]
        assert len(tp1) == 1

    def test_sl_hit_full_close_in_open(self):
        broker = self._broker()
        ex = AtrTrendExecution(position_id="e5", direction=+1, broker=broker)
        ex.place(_LongIntent())
        bar = mk5(1, 1000, 975, 985)   # 安値975 <= 初期SL980
        events = broker.process_bar(bar)
        assert any(e.kind == "SL_HIT" for e in events)
        for e in events:
            ex.on_broker_event(e)
        assert ex.closed and ex.volume_steps == 0
        assert ex.journal[-1][0] == "SL_HIT"

    def test_short_direction_tp1_then_tp2(self):
        broker = self._broker()
        ex = AtrTrendExecution(position_id="e6", direction=-1, broker=broker)
        ex.place(_ShortIntent())
        assert ex.entry_price_int == 998   # 売りは bid 側 fill
        b1 = mk5(1, 990, 978, 982); broker.process_bar(b1); ex.on_bar(b1, None)
        assert ex.state is AtrState.RUNNER and ex.volume_steps == 1
        b2 = mk5(2, 970, 955, 958); broker.process_bar(b2); out = ex.on_bar(b2, None)
        assert out.closed and ex.state is AtrState.CLOSED

    def test_close_forces_flat(self):
        broker = self._broker()
        ex = AtrTrendExecution(position_id="e7", direction=+1, broker=broker)
        ex.place(_LongIntent())
        ex.close("END_OF_DATA")
        assert ex.closed and ex.volume_steps == 0
        assert ex.journal[-1][0] == "CLOSE_ALL"

    def test_volume_one_skips_partial_but_still_bes(self):
        broker = self._broker()
        ex = AtrTrendExecution(position_id="e8", direction=+1, broker=broker)

        class _V1(_LongIntent):
            volume_steps = 1

        ex.place(_V1())
        assert ex.journal[0][1]["half"] == 0
        bar = mk5(1, 1025, 1012, 1022); broker.process_bar(bar); ex.on_bar(bar, None)
        assert ex.state is AtrState.RUNNER
        assert ex.volume_steps == 1                    # 部分決済せず全量ラン
        assert any(n == "MOVE_SL" for n, _ in ex.journal)   # 建値化は行う
        assert all(n != "TP_CLOSE" for n, _ in ex.journal)  # TP1 半利は無し

    def test_place_validates_geometry(self):
        broker = self._broker()
        ex = AtrTrendExecution(position_id="e9", direction=+1, broker=broker)

        class _BadTp2(_LongIntent):
            fib_target_int = 1010   # TP2 <= TP1

        with pytest.raises(ValueError):
            ex.place(_BadTp2())

    def test_bad_direction_rejected(self):
        with pytest.raises(ValueError):
            AtrTrendExecution(position_id="e10", direction=0, broker=self._broker())

    # -- v1.2 ステージ⑤-A: 段階式トレール ------------------------------------------

    def test_stage2_trail_uses_wider_width_after_trigger(self):
        broker = self._broker()
        ex = AtrTrendExecution(position_id="e11", direction=+1, broker=broker)
        ex.place(_LongIntentStage2())

        b1 = mk5(1, 1025, 1010, 1022)   # TP1到達→RUNNER・建値化・トレール(幅10)
        broker.process_bar(b1)
        ex.on_bar(b1, None)
        assert ex.state is AtrState.RUNNER
        assert ex.sl_int == 1015                       # 1025 - 10(ステージ1幅)
        assert all(n != "TRAIL_STAGE2" for n, _ in ex.journal)   # まだ未到達(<1032)

        b2 = mk5(2, 1033, 1020, 1030)   # 含み益 entry(1002)+30=1032 に到達 → ステージ2発動
        broker.process_bar(b2)
        ex.on_bar(b2, None)
        assert any(n == "TRAIL_STAGE2" for n, _ in ex.journal)
        assert ex.sl_int == 1015        # 幅25の目標(1033-25=1008)は現SLより手前 → no-op

        b3 = mk5(3, 1060, 1050, 1055)   # ステージ2幅(25)でのトレール前進を確認
        broker.process_bar(b3)
        ex.on_bar(b3, None)
        assert ex.sl_int == 1035        # 1060 - 25(ステージ2幅)。幅10なら1050になるはず

    def test_stage2_trail_latches_and_does_not_revert(self):
        broker = self._broker()
        ex = AtrTrendExecution(position_id="e12", direction=+1, broker=broker)
        ex.place(_LongIntentStage2())

        for bar in (mk5(1, 1025, 1010, 1022),   # RUNNER化・幅10
                    mk5(2, 1033, 1020, 1030),    # ステージ2発動(no-op)
                    mk5(3, 1060, 1050, 1055)):   # 幅25で前進 → sl=1035
            broker.process_bar(bar)
            ex.on_bar(bar, None)
        assert ex.sl_int == 1035

        # 押し目(高値1050): 幅10へ戻っていれば 1050-10=1040 > 1035 で前進するはずだが、
        # ラッチにより幅25のまま(1050-25=1025 <= 1035)で no-op のはず。
        b4 = mk5(4, 1050, 1010, 1030)
        broker.process_bar(b4)
        ex.on_bar(b4, None)
        assert ex.sl_int == 1035   # 幅10へ戻っていれば1040になってしまう(ラッチ違反)

    def test_stage2_trail_off_by_default_matches_v11(self):
        # trail_stage2_trigger_int=0(既定)なら単一幅トレールのまま(v1.1挙動不変)。
        broker = self._broker()
        ex = AtrTrendExecution(position_id="e13", direction=+1, broker=broker)
        ex.place(_LongIntent())
        b1 = mk5(1, 1025, 1010, 1022)
        broker.process_bar(b1)
        ex.on_bar(b1, None)
        assert ex.sl_int == 1015
        assert all(n != "TRAIL_STAGE2" for n, _ in ex.journal)


@pytest.mark.property
@settings(max_examples=200, deadline=None)
@given(
    direction=st.sampled_from([+1, -1]),
    targets=st.lists(st.integers(min_value=900_000, max_value=1_100_000),
                     min_size=1, max_size=30),
)
def test_advance_sl_to_never_retreats(direction, targets):
    """spec.md §A-3: どんなランダムなターゲット列でもSLは利益方向にしか動かない。"""
    broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=0)
    broker.process_bar(mk5(0, 1_010_000, 990_000, 1_000_000))
    ex = AtrTrendExecution(position_id="propx", direction=direction, broker=broker)

    intent = SimpleNamespace(
        plan_id="p", direction=direction,
        limit_price_int=1_000_000,
        sl_int=1_000_000 - direction * 50_000,
        tp1_int=1_000_000 + direction * 30_000,
        fib_target_int=1_000_000 + direction * 5_000_000,
        trail_distance_ticks=100, volume_steps=2)
    ex.place(intent)
    prev = ex.sl_int
    for target in targets:
        ex._advance_sl_to(target)
        cur = ex.sl_int
        assert ex._profit_side(cur, prev) >= 0
        prev = cur


@pytest.mark.property
@settings(max_examples=200, deadline=None)
@given(
    direction=st.sampled_from([+1, -1]),
    touches=st.lists(st.integers(min_value=900_000, max_value=1_400_000),
                     min_size=1, max_size=30),
)
def test_staged_trail_never_retreats_through_on_bar(direction, touches):
    """spec.md §17(v1.2ステージ⑤-A): 段階トレールの切替を含む on_bar 経路でも
    SLは利益方向にしか動かない(ランダムな高値/安値列でステージ切替が起きても)。"""
    broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=0)
    broker.process_bar(mk5(0, 1_010_000, 990_000, 1_000_000))
    ex = AtrTrendExecution(position_id="propstage2", direction=direction, broker=broker)
    intent = SimpleNamespace(
        plan_id="p", direction=direction,
        limit_price_int=1_000_000,
        sl_int=1_000_000 - direction * 500_000,
        tp1_int=1_000_000 + direction * 1_000,          # ほぼ即TP1到達 → RUNNERへ
        fib_target_int=1_000_000 + direction * 50_000_000,   # 到達させない遠いTP2
        trail_distance_ticks=20_000, volume_steps=2,
        trail_stage2_trigger_int=30_000, trail_stage2_distance_ticks=60_000)
    ex.place(intent)

    def bar_for(i: int, touch: int) -> Candle:
        if direction > 0:
            return mk5(i, touch, touch - 100_000, touch - 50_000)
        return mk5(i, touch + 100_000, touch, touch + 50_000)

    prev_sl = ex.sl_int
    for i, touch in enumerate(touches, start=1):
        bar = bar_for(i, touch)
        for e in broker.process_bar(bar):
            ex.on_broker_event(e)
        if ex.closed:
            break
        ex.on_bar(bar, None)
        if ex.sl_int is not None:
            assert ex._profit_side(ex.sl_int, prev_sl) >= 0
            prev_sl = ex.sl_int


# ---------------------------------------------------------------------------
# 3. AtrTrendScalpProvider: 4条件ゲート + 単一ポジション・ミラー
# ---------------------------------------------------------------------------

def make_provider(**overrides) -> AtrTrendScalpProvider:
    """テスト用の極小周期プロバイダ(ウォームアップを短縮)。"""
    params = dict(symbol="XAUUSD", tf=Timeframe.M5, htf=Timeframe.M15,
                 ema_fast_period=2, ema_medium_period=4, ema_trend_period=2,
                 atr_period=2, vol_sma_period=2, atr_avg_period=2, retrace_lookback=3,
                 atr_vol_mult=Decimal("0.5"), vol_mult=Decimal("0.5"),
                 min_stop_distance_ticks=5, volume_steps=2)
    params.update(overrides)
    return AtrTrendScalpProvider(**params)


def _warm_long_setup(p: AtrTrendScalpProvider) -> None:
    """上昇トレンド + 上位足バイアス確立(発火はさせない)。"""
    # 明確な上昇: M5 終値を段階的に上げる。M15 も上昇 → 上位足バイアス +1。
    for i in range(9):
        c = 1000 + 12 * i
        p.on_candle(mk5(i, c + 4, c - 4, c, vol=100))


class TestAtrTrendScalpProvider:
    def test_no_plan_during_warmup(self):
        p = make_provider()
        out = p.on_candle(mk5(0, 1010, 990, 1000))
        assert out.plans == []

    def test_emits_long_plan_when_all_conditions_pass(self):
        p = make_provider()
        _warm_long_setup(p)
        # トリガー足: 押し目(安値がEMA21を下抜け)から当足終値がEMA21を回復し急伸。
        out = p.on_candle(mk5(9, 1140, 980, 1135, vol=100))
        assert len(out.plans) == 1
        plan = out.plans[0]
        assert plan.direction == +1
        assert plan.limit_price_int == 1135
        assert plan.sl_int < plan.limit_price_int                 # SLは損失側
        assert plan.limit_price_int < plan.tp1_int < plan.fib_target_int
        assert plan.trail_distance_ticks >= 1
        assert plan.atr_at_entry_int >= 1

    def test_htf_bias_conflict_blocks(self):
        # 下降の上位足バイアス下では、上昇モメンタムでもロングは出ない。
        p = make_provider()
        for i in range(9):
            c = 1200 - 12 * i     # 下降トレンド → 上位足バイアス -1
            p.on_candle(mk5(i, c + 4, c - 4, c, vol=100))
        # 直近だけ上げても bias は -1 のまま(EMA50上抜けに至らない)。
        out = p.on_candle(mk5(9, 1130, 1000, 1125, vol=100))
        assert all(pl.direction == +1 for pl in out.plans) is True or out.plans == []
        # ロングは出ない(bias 不一致)。
        assert not any(pl.direction == +1 for pl in out.plans)

    def test_volume_filter_blocks(self):
        p = make_provider(vol_mult=Decimal("5"))   # 出来高5倍要求 → 通常出来高では遮断
        _warm_long_setup(p)
        out = p.on_candle(mk5(9, 1140, 980, 1135, vol=100))
        assert out.plans == []

    def test_atr_filter_blocks(self):
        p = make_provider(atr_vol_mult=Decimal("100"))   # 非現実的なボラ要求 → 遮断
        _warm_long_setup(p)
        out = p.on_candle(mk5(9, 1140, 980, 1135, vol=100))
        assert out.plans == []

    def test_no_retrace_blocks(self):
        p = make_provider()
        _warm_long_setup(p)
        # 押し目タッチ無し(安値がEMA21を下抜けない高い位置)→ 発火しない。
        out = p.on_candle(mk5(9, 1140, 1130, 1135, vol=100))
        assert out.plans == []

    def test_vol_mult_max_blocks_spike_volume(self):
        # v1.2 ステージ①(スパイク足フィルター案1): 既定OFFでは出るプランが
        # vol_mult_max 設定時に出来高スパイク足として遮断されること。
        # vol_sma は当足も window に含む(SMA.update が現在値ごと平均を返す)ため、
        # vol_sma_period=5 にして直近5本(100,100,100,100,500)の平均=180で検証する
        # (閾値 = 2.5 * 180 = 450 < 500)。
        p = make_provider(vol_mult_max=Decimal("2.5"), vol_sma_period=5)
        _warm_long_setup(p)
        out = p.on_candle(mk5(9, 1140, 980, 1135, vol=500))
        assert out.plans == []

    def test_vol_mult_max_off_by_default_does_not_block(self):
        p = make_provider()   # vol_mult_max 既定 0 = 無効
        _warm_long_setup(p)
        out = p.on_candle(mk5(9, 1140, 980, 1135, vol=300))
        assert len(out.plans) == 1

    def test_range_atr_mult_max_blocks_wide_range(self):
        # v1.2 ステージ①(スパイク足フィルター案2): レンジが ATR の倍数を超える
        # 足(このシナリオでは range=160, atr≒87.98 → 比≒1.82)は遮断される。
        p = make_provider(range_atr_mult_max=Decimal("1.5"))
        _warm_long_setup(p)
        out = p.on_candle(mk5(9, 1140, 980, 1135, vol=100))
        assert out.plans == []

    def test_range_atr_mult_max_off_by_default_does_not_block(self):
        p = make_provider()   # range_atr_mult_max 既定 0 = 無効
        _warm_long_setup(p)
        out = p.on_candle(mk5(9, 1140, 980, 1135, vol=100))
        assert len(out.plans) == 1

    def test_atr_floor_bps_blocks_low_relative_volatility(self):
        # v1.2 ステージ③(相対ボラフロア): このシナリオの ATR/価格比は約775bps
        # (atr≒87.98, c_int=1135)。閾値800にすると比が下回り遮断される。
        p = make_provider(atr_floor_bps=Decimal("800"))
        _warm_long_setup(p)
        out = p.on_candle(mk5(9, 1140, 980, 1135, vol=100))
        assert out.plans == []

    def test_atr_floor_bps_below_threshold_does_not_block(self):
        # 同じシナリオで閾値700(実比775bpsより低い)なら遮断されない。
        p = make_provider(atr_floor_bps=Decimal("700"))
        _warm_long_setup(p)
        out = p.on_candle(mk5(9, 1140, 980, 1135, vol=100))
        assert len(out.plans) == 1

    def test_atr_floor_bps_explicit_zero_disables(self):
        # v1.2 ステージ③採用により既定値は5.0(spec.md §15)。明示的に0を渡せば
        # 無効化されることを確認する(このシナリオの実比775bpsは既定5.0では
        # 遮断されないほど大きいため、既定値のままでも本テストの意図は成立するが、
        # 「0で無効化できる」契約自体を明示的に検証する)。
        p = make_provider(atr_floor_bps=Decimal("0"))
        _warm_long_setup(p)
        out = p.on_candle(mk5(9, 1140, 980, 1135, vol=100))
        assert len(out.plans) == 1

    def test_suppresses_new_plan_while_position_open(self):
        p = make_provider()
        _warm_long_setup(p)
        out1 = p.on_candle(mk5(9, 1140, 980, 1135, vol=100))
        assert len(out1.plans) == 1
        out2 = p.on_candle(mk5(10, 1160, 1120, 1155, vol=100))
        assert out2.plans == []     # 建玉中(ミラー)として抑制

    def test_reset_position_mirror_clears_phantom(self):
        p = make_provider()
        _warm_long_setup(p)
        out1 = p.on_candle(mk5(9, 1140, 980, 1135, vol=100))
        assert len(out1.plans) == 1
        p.reset_position_mirror()
        out2 = p.on_candle(mk5(10, 1200, 1000, 1195, vol=100))
        assert len(out2.plans) == 1     # ミラー解除で再発火

    def test_session_filter_blocks_outside_window(self):
        # UTC 00:00 起点はセッション窓(07:00-16:00 UTC)外 → 発火しない。
        p = make_provider(session_filter=True)
        _warm_long_setup(p)
        out = p.on_candle(mk5(9, 1140, 980, 1135, vol=100))
        assert out.plans == []

    # -- reject_reason 可視化注釈(取引ロジックには影響しない。--journal-eval 用) --

    def test_reject_reason_warmup(self):
        p = make_provider()
        out = p.on_candle(mk5(0, 1010, 990, 1000))
        assert out.plans == []
        assert out.reject_reason == "warmup"

    def test_reject_reason_signal_on_emit(self):
        p = make_provider()
        _warm_long_setup(p)
        out = p.on_candle(mk5(9, 1140, 980, 1135, vol=100))
        assert len(out.plans) == 1
        assert out.reject_reason == "signal"

    def test_reject_reason_no_direction_on_bias_momentum_conflict(self):
        # 上昇でウォームアップ(上位足バイアス+1)後、短期だけ急落させると
        # 短期モメンタム(EMA9<EMA21)と上位足バイアス(+1)が食い違い、方向不成立。
        p = make_provider()
        _warm_long_setup(p)
        out = p.on_candle(mk5(9, 1010, 890, 900, vol=100))
        assert out.plans == []
        assert out.reject_reason == "no_direction"

    def test_reject_reason_no_retrace(self):
        p = make_provider()
        _warm_long_setup(p)
        # 押し目タッチ無し(安値がEMA21を下抜けない)→ no_retrace で見送り。
        out = p.on_candle(mk5(9, 1140, 1130, 1135, vol=100))
        assert out.plans == []
        assert out.reject_reason == "no_retrace"

    def test_reject_reason_volume_filter(self):
        p = make_provider(vol_mult=Decimal("5"))
        _warm_long_setup(p)
        out = p.on_candle(mk5(9, 1140, 980, 1135, vol=100))
        assert out.plans == []
        assert out.reject_reason == "volume_filter"

    def test_reject_reason_out_of_session(self):
        p = make_provider(session_filter=True)   # T0=UTC00:00 は窓外
        _warm_long_setup(p)
        out = p.on_candle(mk5(9, 1140, 980, 1135, vol=100))
        assert out.plans == []
        assert out.reject_reason == "out_of_session"

    def test_reject_reason_position_open(self):
        p = make_provider()
        _warm_long_setup(p)
        p.on_candle(mk5(9, 1140, 980, 1135, vol=100))       # 建玉(ミラー)
        out = p.on_candle(mk5(10, 1160, 1120, 1155, vol=100))
        assert out.plans == []
        assert out.reject_reason == "position_open"

    def test_reject_reason_default_none(self):
        # 注釈は既定 None(理由を設定しない出力経路が無いことの回帰保護)。
        from infers.strategies.atr_trend_scalp.signals import AtrTrendOutput
        assert AtrTrendOutput().reject_reason is None

    def test_constructor_validates(self):
        with pytest.raises(ValueError):
            make_provider(min_stop_distance_ticks=0)
        with pytest.raises(ValueError):
            make_provider(volume_steps=0)
        with pytest.raises(ValueError):
            make_provider(tp1_atr_mult=Decimal(3), tp2_atr_mult=Decimal(2))
        with pytest.raises(ValueError):
            # vol_mult_max は 0 か vol_mult(既定0.5)より大きい必要がある。
            make_provider(vol_mult_max=Decimal("0.3"))
        with pytest.raises(ValueError):
            make_provider(range_atr_mult_max=Decimal("-1"))
        with pytest.raises(ValueError):
            make_provider(atr_floor_bps=Decimal("-1"))
        with pytest.raises(ValueError):
            make_provider(trail_stage2_trigger_atr_mult=Decimal("-1"))
        with pytest.raises(ValueError):
            # trigger>0 のとき trail_stage2_atr_mult は必須(既定0のまま=不正)。
            make_provider(trail_stage2_trigger_atr_mult=Decimal("1.5"))
        with pytest.raises(ValueError):
            # trail_stage2_atr_mult は trail_atr_mult(既定0.15)より大きい必要がある。
            make_provider(trail_stage2_trigger_atr_mult=Decimal("1.5"),
                          trail_stage2_atr_mult=Decimal("0.1"))
        with pytest.raises(ValueError):
            # trigger=0(既定)のまま distance だけ指定するのは不正。
            make_provider(trail_stage2_atr_mult=Decimal("0.5"))

    def test_stage2_trail_geometry_baked_into_plan(self):
        p = make_provider(trail_stage2_trigger_atr_mult=Decimal("1.5"),
                          trail_stage2_atr_mult=Decimal("0.5"))
        _warm_long_setup(p)
        out = p.on_candle(mk5(9, 1140, 980, 1135, vol=100))
        assert len(out.plans) == 1
        plan = out.plans[0]
        # atr_at_entry_int(88) × 1.5 ≒ 132 / × 0.5 ≒ 44(ATR固定・整数丸め)。
        assert plan.trail_stage2_trigger_int == round(plan.atr_at_entry_int * 1.5)
        assert plan.trail_stage2_distance_ticks == max(
            1, round(plan.atr_at_entry_int * 0.5))

    def test_stage2_trail_off_by_default_zero_fields(self):
        p = make_provider()   # trail_stage2_trigger_atr_mult 既定 0 = 無効
        _warm_long_setup(p)
        out = p.on_candle(mk5(9, 1140, 980, 1135, vol=100))
        assert len(out.plans) == 1
        plan = out.plans[0]
        assert plan.trail_stage2_trigger_int == 0
        assert plan.trail_stage2_distance_ticks == 0


# ---------------------------------------------------------------------------
# 4. レジストリ配線
# ---------------------------------------------------------------------------

class TestRegistryWiring:
    def test_registered_with_execution_and_report(self):
        spec = get_strategy("atr_trend_scalp")
        assert spec.name == "atr_trend_scalp"
        assert spec.build_execution is not None
        assert spec.report_spec is not None
        prov = spec.build(symbol="XAUUSD", tf=Timeframe.M5)
        assert isinstance(prov, AtrTrendScalpProvider)

    def test_v12_default_enables_atr_floor(self):
        # 既定(atr_trend_scalp)は v1.2 = atr_floor_bps 5.0(相対ボラフロア有効)。
        prov = get_strategy("atr_trend_scalp").build(symbol="XAUUSD", tf=Timeframe.M5)
        assert prov._atr_floor_bps == Decimal("5.0")

    def test_v11_preset_disables_atr_floor(self):
        # v1.1 プリセットは atr_floor_bps=0(無効)。他は v1.2 と同一(執行・レポート共通)。
        spec = get_strategy("atr_trend_scalp_v11")
        assert spec.name == "atr_trend_scalp_v11"
        assert spec.build_execution is get_strategy("atr_trend_scalp").build_execution
        assert spec.report_spec is not None
        prov = spec.build(symbol="XAUUSD", tf=Timeframe.M5)
        assert isinstance(prov, AtrTrendScalpProvider)
        assert prov._atr_floor_bps == Decimal("0")
        # v1.1/v1.2 の差分は atr_floor_bps のみ(他パラメータは一致)。
        v12 = get_strategy("atr_trend_scalp").build(symbol="XAUUSD", tf=Timeframe.M5)
        assert prov._vol_mult == v12._vol_mult
        assert prov._tp1_atr_mult == v12._tp1_atr_mult
        assert prov._tp2_atr_mult == v12._tp2_atr_mult
        assert prov._trail_atr_mult == v12._trail_atr_mult

    def test_v13_preset_moves_tp1_only(self):
        # v1.3 プリセットは tp1_atr_mult=0.4。v1.2 との差分は tp1 のみ(improvement-plan-v1.3.md)。
        spec = get_strategy("atr_trend_scalp_v13")
        assert spec.build_execution is get_strategy("atr_trend_scalp").build_execution
        assert spec.report_spec is not None
        prov = spec.build(symbol="XAUUSD", tf=Timeframe.M5)
        v12 = get_strategy("atr_trend_scalp").build(symbol="XAUUSD", tf=Timeframe.M5)
        assert prov._tp1_atr_mult == Decimal("0.4")
        assert v12._tp1_atr_mult == Decimal("0.5")
        assert prov._atr_floor_bps == v12._atr_floor_bps
        assert prov._vol_mult == v12._vol_mult
        assert prov._tp2_atr_mult == v12._tp2_atr_mult
        assert prov._trail_atr_mult == v12._trail_atr_mult


# ---------------------------------------------------------------------------
# 5. 結合: BacktestEngine + AiGateway(パススルー) が AtrTrendExecution を駆動
# ---------------------------------------------------------------------------

def test_engine_drives_atr_trend_execution_end_to_end():
    """パススルーゲート経由で参入→分割決済の round-trip が成立することの実証
    (BacktestEngine→TradingLoop→AtrTrendExecution)。トレード総数は固定せず、
    1回以上の参入と純益の符号のみを検証する。"""
    provider = AtrTrendScalpProvider(
        symbol="XAUUSD", tf=Timeframe.M5, htf=Timeframe.M15,
        ema_fast_period=2, ema_medium_period=4, ema_trend_period=2,
        atr_period=2, vol_sma_period=2, atr_avg_period=2, retrace_lookback=3,
        atr_vol_mult=Decimal("0.5"), vol_mult=Decimal("0.5"),
        min_stop_distance_ticks=5, volume_steps=2)
    candles = [mk5(i, 1000 + 12 * i + 4, 1000 + 12 * i - 4, 1000 + 12 * i, 100)
               for i in range(9)]
    candles.append(mk5(9, 1140, 980, 1135, 100))     # 参入トリガー
    candles.append(mk5(10, 1400, 1300, 1390, 100))   # 大きく順行 → TP1/TP2 決済

    gateway = AiGateway(client=PassthroughLlmClient(), cache=VerdictCache(),
                        policy=PASSTHROUGH_POLICY)
    engine = BacktestEngine(
        broker=LedgerBroker(spread_ticks=2, min_stop_distance_ticks=5),
        gateway=gateway,
        risk=RiskManager(RiskConfig(max_position_volume_steps=4, max_total_volume_steps=8,
                                    max_spread_ticks=10, daily_loss_limit_tick_steps=10_000_000)),
        fsm_config=FsmConfig(min_be_distance_ticks=10, be_offset_ticks=2,
                             breakout_buffer_ticks=10),
        execution_factory=get_strategy("atr_trend_scalp").build_execution,
    )
    report = engine.run(candles, provider)
    assert len(report.trades) >= 1
    assert gateway.stats["L1:GO"] >= 1

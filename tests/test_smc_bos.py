"""smc_bos 手法 (段階S2): 構造検出・BOS判定・執行モデル・分析層・レジストリ配線・
結合(BacktestEngine→TradingLoop→SmcExecution)の検証。spec.md 参照。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

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
from infers.strategies.smc_bos.execution import SmcExecution, SmcState
from infers.strategies.smc_bos.provider import SmcBosProvider
from infers.strategies.smc_bos.structure import SwingDetector, SwingPoint, bos_direction

UTC = timezone.utc
T0 = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


def mk(i: int, h: int, l: int, c: int, tf: Timeframe = Timeframe.M30) -> Candle:
    o = max(l, min(h, c))
    return Candle(symbol="XAUUSD", tf=tf, open_time=T0 + i * tf.duration,
                  o_int=o, h_int=h, l_int=l, c_int=c, volume=1, is_closed=True)


def mk_unclosed(i: int, h: int, l: int, c: int) -> Candle:
    o = max(l, min(h, c))
    return Candle(symbol="XAUUSD", tf=Timeframe.M30, open_time=T0 + i * Timeframe.M30.duration,
                  o_int=o, h_int=h, l_int=l, c_int=c, volume=1, is_closed=False)


# ---------------------------------------------------------------------------
# 1. SwingDetector: 確定遅延つきピボット検出
# ---------------------------------------------------------------------------

class TestSwingDetector:
    def test_rejects_unclosed_candle(self):
        with pytest.raises(ValueError, match="closed candles only"):
            SwingDetector(1).update(mk_unclosed(0, 1010, 990, 1000))

    def test_rejects_out_of_order(self):
        det = SwingDetector(1)
        det.update(mk(5, 1010, 990, 1000))
        with pytest.raises(ValueError, match="strictly increasing"):
            det.update(mk(4, 1010, 990, 1000))

    def test_rejects_mixed_series(self):
        det = SwingDetector(1)
        det.update(mk(0, 1010, 990, 1000))
        other = Candle(symbol="EURUSD", tf=Timeframe.M30,
                       open_time=T0 + Timeframe.M30.duration,
                       o_int=1000, h_int=1010, l_int=990, c_int=1000,
                       volume=1, is_closed=True)
        with pytest.raises(ValueError, match="mixed series"):
            det.update(other)

    def test_empty_until_window_full(self):
        det = SwingDetector(2)   # window = 5
        for i in range(4):
            assert det.update(mk(i, 1000 + i, 990 + i, 995 + i)) == []

    def test_confirms_pivot_high_with_delay(self):
        det = SwingDetector(1)   # window = 3
        assert det.update(mk(0, 1010, 990, 1000)) == []
        assert det.update(mk(1, 1030, 995, 1010)) == []   # 中央候補 (高値1030)
        swings = det.update(mk(2, 1015, 985, 995))         # 右側確認バー
        assert len(swings) == 1
        sp = swings[0]
        assert isinstance(sp, SwingPoint)
        assert sp.kind == "HIGH"
        assert sp.price_int == 1030
        assert sp.bar_time == T0 + 1 * Timeframe.M30.duration
        assert sp.confirmed_at == mk(2, 1015, 985, 995).close_time

    def test_confirms_pivot_low_with_delay(self):
        det = SwingDetector(1)
        det.update(mk(0, 1010, 990, 1000))
        det.update(mk(1, 1005, 970, 985))     # 中央候補 (安値970)
        swings = det.update(mk(2, 1000, 980, 995))
        assert len(swings) == 1
        assert swings[0].kind == "LOW"
        assert swings[0].price_int == 970

    def test_tie_is_not_confirmed(self):
        """厳密な不等号 (`<`) のため、隣接バーと同値の極値はピボット扱いしない。"""
        det = SwingDetector(1)
        det.update(mk(0, 1030, 990, 1000))     # 高値1030 (中央と同値)
        det.update(mk(1, 1030, 995, 1010))     # 中央候補 (高値1030 — タイ)
        swings = det.update(mk(2, 1015, 985, 995))
        assert swings == []

    def test_double_pivot_returns_both(self):
        """中央バーが最高高値かつ最安安値なら HIGH/LOW を両方返す。"""
        det = SwingDetector(1)
        det.update(mk(0, 1010, 1000, 1005))
        det.update(mk(1, 1050, 950, 1000))     # 高値・安値ともに極端
        swings = det.update(mk(2, 1020, 990, 1005))
        kinds = {s.kind for s in swings}
        assert kinds == {"HIGH", "LOW"}


# ---------------------------------------------------------------------------
# 2. bos_direction: 純粋関数のBOS判定
# ---------------------------------------------------------------------------

class TestBosDirection:
    def test_bull_breakout(self):
        assert bos_direction(1040, swing_high=1030, swing_low=None, buffer_ticks=0) == +1

    def test_bear_breakout(self):
        assert bos_direction(860, swing_high=None, swing_low=900, buffer_ticks=0) == -1

    def test_no_breakout_within_band(self):
        assert bos_direction(1000, swing_high=1030, swing_low=900, buffer_ticks=0) == 0

    def test_buffer_widens_required_break(self):
        assert bos_direction(1035, swing_high=1030, swing_low=None, buffer_ticks=10) == 0
        assert bos_direction(1041, swing_high=1030, swing_low=None, buffer_ticks=10) == +1

    def test_no_swings_yet_returns_zero(self):
        assert bos_direction(1000, swing_high=None, swing_low=None, buffer_ticks=0) == 0

    def test_negative_buffer_rejected(self):
        with pytest.raises(ValueError):
            bos_direction(1000, swing_high=900, swing_low=None, buffer_ticks=-1)


# ---------------------------------------------------------------------------
# 3. SmcExecution: 成行参入 + 固定SL/RR利確 (段階S2)
# ---------------------------------------------------------------------------

class TestSmcExecution:
    def _broker(self) -> SimBroker:
        b = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        b.process_bar(mk(0, 1005, 995, 1000))
        return b

    def test_market_entry_then_tp_close(self):
        broker = self._broker()
        ex = SmcExecution(position_id="s1", direction=+1, broker=broker)

        class _Intent:
            limit_price_int = 1000
            sl_int = 980
            fib_target_int = 1040
            volume_steps = 2

        ex.place(_Intent())
        assert ex.state is SmcState.OPEN
        assert ex.entry_price_int == 1002

        bar1 = mk(1, 1030, 1010, 1025)
        broker.process_bar(bar1)
        assert ex.on_bar(bar1, None).closed is False

        bar2 = mk(2, 1045, 1020, 1042)
        broker.process_bar(bar2)
        out = ex.on_bar(bar2, None)
        assert out.closed and ex.state is SmcState.CLOSED
        assert ex.volume_steps == 0
        assert ex.journal[-1][0] == "TP_CLOSE"

    def test_market_entry_then_sl_hit(self):
        broker = self._broker()
        ex = SmcExecution(position_id="s2", direction=+1, broker=broker)

        class _Intent:
            limit_price_int = 1000
            sl_int = 980
            fib_target_int = 1040
            volume_steps = 2

        ex.place(_Intent())
        bar1 = mk(1, 1000, 975, 985)
        events = broker.process_bar(bar1)
        assert any(e.kind == "SL_HIT" for e in events)
        for e in events:
            ex.on_broker_event(e)
        assert ex.closed and ex.state is SmcState.CLOSED
        assert ex.journal[-1][0] == "SL_HIT"

    def test_short_direction_tp(self):
        broker = self._broker()
        ex = SmcExecution(position_id="s3", direction=-1, broker=broker)

        class _Intent:
            limit_price_int = 1000
            sl_int = 1020
            fib_target_int = 960
            volume_steps = 2

        ex.place(_Intent())
        assert ex.state is SmcState.OPEN
        bar = mk(1, 990, 955, 965)
        broker.process_bar(bar)
        assert ex.on_bar(bar, None).closed
        assert ex.journal[-1][0] == "TP_CLOSE"

    def test_close_forces_flat(self):
        broker = self._broker()
        ex = SmcExecution(position_id="s4", direction=+1, broker=broker)

        class _Intent:
            limit_price_int = 1000
            sl_int = 980
            fib_target_int = 1040
            volume_steps = 2

        ex.place(_Intent())
        ex.close("END_OF_DATA")
        assert ex.closed and ex.volume_steps == 0
        assert ex.journal[-1][0] == "CLOSE_ALL"


# ---------------------------------------------------------------------------
# 4. SmcBosProvider: BOS + EMA80フィルタ + 単一ポジション制約
# ---------------------------------------------------------------------------

def make_provider(**overrides) -> SmcBosProvider:
    params = dict(symbol="XAUUSD", tf=Timeframe.M30, ema_period=2, atr_period=2,
                 swing_lookback=50, breakout_buffer_atr=Decimal(0), breakout_buffer_ticks=0,
                 sl_buffer_ticks=0, atr_sl_mult=Decimal(0), min_stop_distance_ticks=5,
                 rr_target=Decimal(2), volume_steps=2)
    params.update(overrides)
    return SmcBosProvider(**params)


class TestSmcBosProvider:
    def test_no_plan_during_warmup(self):
        p = make_provider()
        p._swing_high = 900   # 注入してもウォームアップ中はEMA/ATR未準備で発火しない
        out = p.on_candle(mk(0, 1010, 990, 1000))
        assert out.plans == []

    def test_emits_bull_plan_on_bos_with_ema_pass(self):
        p = make_provider()
        p._swing_high = 1030
        p._swing_low = 970
        p.on_candle(mk(0, 1010, 990, 1000))
        p.on_candle(mk(1, 1020, 995, 1010))   # EMA/ATR ready (seed=1005 / 22.5)
        out = p.on_candle(mk(2, 1045, 1025, 1040))   # 最高値の終値 → EMAフィルタ自動通過
        assert len(out.plans) == 1
        plan = out.plans[0]
        assert plan.direction == +1
        assert plan.limit_price_int == 1040
        assert plan.sl_int == 970
        assert plan.fib_target_int == 1180
        assert plan.volume_steps == 2

    def test_emits_bear_plan_on_bos_with_ema_pass(self):
        p = make_provider()
        p._swing_high = 1100
        p._swing_low = 900
        p.on_candle(mk(0, 1010, 990, 1000))
        p.on_candle(mk(1, 1005, 985, 995))
        out = p.on_candle(mk(2, 910, 850, 860))   # 最安値の終値 → EMAフィルタ自動通過
        assert len(out.plans) == 1
        plan = out.plans[0]
        assert plan.direction == -1
        assert plan.limit_price_int == 860
        assert plan.sl_int == 1100
        assert plan.fib_target_int == 380

    def test_ema_filter_blocks_bos_when_close_below_ema(self):
        """BOSは成立するがEMAフィルタで遮断される (買い: 終値<EMA)。"""
        p = make_provider(swing_lookback=50)
        p._swing_high = 900   # 低い位置に注入し BOS だけは易々と成立させる
        p.on_candle(mk(0, 1110, 1090, 1100))
        p.on_candle(mk(1, 1110, 1090, 1100))   # EMA seed = 1100
        out = p.on_candle(mk(2, 1010, 990, 1000))   # close=1000>900(BOS成立) だがEMA(~1033)未満
        assert out.plans == []

    def test_suppresses_new_plan_while_position_open(self):
        p = make_provider()
        p._swing_high = 1030
        p._swing_low = 970
        p.on_candle(mk(0, 1010, 990, 1000))
        p.on_candle(mk(1, 1020, 995, 1010))
        out1 = p.on_candle(mk(2, 1045, 1025, 1040))
        assert len(out1.plans) == 1   # 建玉 (sl=970, tp=1180)

        # 同じBOS+EMA条件を満たす足だが、TP/SLに未到達 → 建玉中として抑制
        out2 = p.on_candle(mk(3, 1100, 1050, 1095))
        assert out2.plans == []

    def test_resumes_after_mirrored_tp_touch(self):
        p = make_provider()
        p._swing_high = 1030
        p._swing_low = 970
        p.on_candle(mk(0, 1010, 990, 1000))
        p.on_candle(mk(1, 1020, 995, 1010))
        p.on_candle(mk(2, 1045, 1025, 1040))           # 建玉 (sl=970, tp=1180)
        # 大きな上ヒゲでTP(1180)に到達するが、終値は安く戻し新規BOSは不成立
        out_touch = p.on_candle(mk(3, 1185, 1000, 1010))
        assert out_touch.plans == []                  # ミラー解除はされるが新規条件は不成立
        # 全履行中で最高値の終値 → EMAフィルタ自動通過、ミラーは解除済みなので発火
        out_resume = p.on_candle(mk(4, 1210, 1150, 1200))
        assert len(out_resume.plans) == 1
        assert out_resume.plans[0].direction == +1
        assert out_resume.plans[0].limit_price_int == 1200

    def test_initial_sl_falls_back_to_atr_when_no_opposite_swing(self):
        """逆側のスイングが未確定 (None) ならATR/最小距離フロアのみで決まる。

        TR0=h0-l0=20 (前終値なし)。TR1=max(25,|1020-1000|=20,|995-1000|=5)=25
        → ATR seed=(20+25)/2=22.5。bar2自身でも更新: TR2=max(20,|1045-1010|=35,
        |1025-1010|=15)=35 → Wilder: (22.5*1+35)/2=28.75。floor=int(2*28.75)
        =int(57.5)=58 (ROUND_HALF_EVEN: 57は奇数→偶数の58へ)。
        """
        p = make_provider(atr_sl_mult=Decimal(2))
        p._swing_high = 1030
        # _swing_low は注入しない (None のまま)
        p.on_candle(mk(0, 1010, 990, 1000))
        p.on_candle(mk(1, 1020, 995, 1010))
        out = p.on_candle(mk(2, 1045, 1025, 1040))
        assert len(out.plans) == 1
        assert out.plans[0].sl_int == 1040 - 58

    def test_initial_sl_uses_atr_floor_when_structure_too_close(self):
        """構造距離がATR/最小距離フロアより小さい場合はフロアが優先される。

        ATR floor=58 (上のテストと同じ系列。コメント参照)。構造距離は
        entry(1040)-(swing_low(1035)-0)=5 のみ → max(5,58,5)=58 でフロアが勝つ。
        """
        p = make_provider(atr_sl_mult=Decimal(2))
        p._swing_high = 1030
        p._swing_low = 1035   # entry(1040)からわずか5ティック → フロア(58)未満
        p.on_candle(mk(0, 1010, 990, 1000))
        p.on_candle(mk(1, 1020, 995, 1010))
        out = p.on_candle(mk(2, 1045, 1025, 1040))
        assert len(out.plans) == 1
        assert out.plans[0].sl_int == 1040 - 58   # 構造距離5ではなくフロア58が採用される

    def test_constructor_validates_params(self):
        with pytest.raises(ValueError):
            make_provider(min_stop_distance_ticks=0)
        with pytest.raises(ValueError):
            make_provider(rr_target=Decimal(0))
        with pytest.raises(ValueError):
            make_provider(volume_steps=0)


# ---------------------------------------------------------------------------
# 5. プロパティテスト: 初期SLは min_stop_distance_ticks より絶対に狭くならない
# ---------------------------------------------------------------------------

@pytest.mark.property
@settings(max_examples=200, deadline=None)
@given(
    direction=st.sampled_from([+1, -1]),
    entry=st.integers(min_value=100_000, max_value=200_000),
    swing_offset=st.integers(min_value=-50, max_value=50),
    atr_val=st.decimals(min_value=Decimal(0), max_value=Decimal(100), places=9),
    has_opposite_swing=st.booleans(),
)
def test_initial_sl_never_tighter_than_min_distance(direction, entry, swing_offset,
                                                     atr_val, has_opposite_swing):
    """spec.md §3.1: final_dist = max(構造距離, atr_sl_mult×ATR, min_stop_distance)。

    構造側スイングが極端 (entryの内側・負の距離) でも、結果のSL距離が
    min_stop_distance_ticks を下回ることは絶対にない。
    """
    p = make_provider(atr_sl_mult=Decimal("1.5"), min_stop_distance_ticks=5, sl_buffer_ticks=0)
    if has_opposite_swing:
        if direction > 0:
            p._swing_low = entry - swing_offset
        else:
            p._swing_high = entry + swing_offset
    sl = p._initial_sl(direction, entry, atr_val)
    dist = abs(entry - sl)
    assert dist >= p._min_stop_distance_ticks
    assert (direction > 0 and sl < entry) or (direction < 0 and sl > entry)


# ---------------------------------------------------------------------------
# 6. レジストリ配線
# ---------------------------------------------------------------------------

class TestRegistryWiring:
    def test_smc_bos_registered_with_execution(self):
        assert "smc_bos" in get_strategy("smc_bos").name
        assert get_strategy("smc_bos").build_execution is not None


# ---------------------------------------------------------------------------
# 7. 結合: BacktestEngine + AiGateway(パススルー) が SmcExecution を駆動
# ---------------------------------------------------------------------------

def test_engine_drives_smc_execution_end_to_end():
    """BOS+EMA80で成行参入し、TP到達で1回目のトレードが確定する
    (BacktestEngine→TradingLoop→SmcExecution の経路が成立することの実証。
    spec.md §5.7: 寛容ゲートは実体の AiGateway+PassthroughLlmClient を使う)。

    TP到達後、終値が依然として最高値圏ならBOS+EMAが再成立し新規プランが
    出ること自体は本手法の正しい挙動(トレンド継続中の再エントリー。spec.md
    §1の391トレード/4年は本来この再発火が前提)なので、トレード総数は固定せず
    「1回目の round-trip が正しく決済されること」のみを検証する。"""
    candles = [
        mk(0, 1010, 990, 1000),
        mk(1, 1030, 995, 1010),     # スイング高値1030の候補
        mk(2, 1015, 985, 995),      # 確定 (swing_high=1030, swing_low=985)
        mk(3, 1045, 1025, 1040),    # BOS+EMA成立 → entry=1040, sl=985, tp=1150
        mk(4, 1160, 1100, 1155),    # 高値1160 >= TP1150 → 決済
    ]
    provider = SmcBosProvider(symbol="XAUUSD", tf=Timeframe.M30, ema_period=2, atr_period=2,
                              swing_lookback=1, breakout_buffer_atr=Decimal(0),
                              breakout_buffer_ticks=0, sl_buffer_ticks=0,
                              atr_sl_mult=Decimal(0), min_stop_distance_ticks=5,
                              rr_target=Decimal(2), volume_steps=2)
    gateway = AiGateway(client=PassthroughLlmClient(), cache=VerdictCache(),
                        policy=PASSTHROUGH_POLICY)
    engine = BacktestEngine(
        broker=LedgerBroker(spread_ticks=2, min_stop_distance_ticks=5),
        gateway=gateway,
        risk=RiskManager(RiskConfig(max_position_volume_steps=4, max_total_volume_steps=8,
                                    max_spread_ticks=10, daily_loss_limit_tick_steps=10_000_000)),
        fsm_config=FsmConfig(min_be_distance_ticks=10, be_offset_ticks=2,
                             breakout_buffer_ticks=10),
        execution_factory=get_strategy("smc_bos").build_execution,
    )
    report = engine.run(candles, provider)

    assert len(report.trades) >= 1
    trade = report.trades[0]
    assert trade.direction == +1
    assert trade.pnl_tick_steps > 0
    assert trade.exit_kind == "CLOSE"          # TPは close_volume 経由 (SLヒットではない)
    assert gateway.stats["L1:GO"] >= 1         # パススルー経由でGUARDRAIL無しに約定したことの証跡

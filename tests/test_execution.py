"""執行層のテスト: 状態機械ライフサイクル・Simブローカー・リスク・SL単調性プロパティ。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from infers.analysis.dow import StructureEvent, StructureEventType, TrendState
from infers.analysis.support_resistance import SRZone
from infers.analysis.zigzag import SwingPoint
from infers.data.models import Candle, Timeframe
from infers.execution.risk import OrderRequest, RiskConfig, RiskManager
from infers.execution.sim_broker import BrokerRejection, SimBroker
from infers.execution.sm import (
    FsmConfig, PositionFSM, PosState, SlMonotonicityError, TransitionError,
)

UTC = timezone.utc
T0 = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
FAR_FUTURE = T0 + timedelta(days=30)
CFG = FsmConfig(min_be_distance_ticks=10, be_offset_ticks=2, breakout_buffer_ticks=10)


def mk_candle(i: int, h: int, l: int, c: int) -> Candle:
    o = max(l, min(h, c))  # o は [l,h] 内であれば内容に影響しない
    return Candle(symbol="XAUUSD", tf=Timeframe.M5,
                  open_time=T0 + i * Timeframe.M5.duration,
                  o_int=o, h_int=h, l_int=l, c_int=c, volume=1, is_closed=True)


def sw(kind: str, price: int, i: int) -> SwingPoint:
    t = T0 + i * Timeframe.M5.duration
    return SwingPoint(kind=kind, bar_time=t, price_int=price, tf=Timeframe.M5,
                      confirmed_at=t + Timeframe.M5.duration)


def structure_event(ev_type: StructureEventType, price: int, i: int = 0) -> StructureEvent:
    kind = "LOW" if ev_type in (StructureEventType.HL, StructureEventType.LL) else "HIGH"
    return StructureEvent(type=ev_type, swing=sw(kind, price, i + 1),
                          prev_swing=sw(kind, price - 5, i), state_after=TrendState.UP)


def make_filled_long(broker: SimBroker, *, fill_low: int = 988) -> PositionFSM:
    """打診約定済み (entry=990, sl=960, vol=2) のロングFSMを組み立てる。"""
    broker.process_bar(mk_candle(0, 1005, 995, 1000))
    fsm = PositionFSM(position_id="pos1", direction=+1, broker=broker, config=CFG)
    fsm.place_probe(limit_price_int=990, volume_steps=2, sl_int=960,
                    expiry=FAR_FUTURE, invalidation_price=950)
    events = broker.process_bar(mk_candle(1, 1000, fill_low, 992))
    assert [e.kind for e in events] == ["FILL"]
    fsm.on_probe_fill(events[0].price_int, events[0].volume_steps)
    return fsm


def make_runner_long(broker: SimBroker) -> PositionFSM:
    """打診→追撃→建値SL→半分利確(RSI) まで進めた RUNNER 状態のロングを返す。

    平均建値 1012, 残玉 2, 建値SL 1014 (= 平均建値+微益2)。
    """
    fsm = make_filled_long(broker)
    bar3 = mk_candle(3, 1035, 1015, 1031)               # W1=1020+buffer10 突破
    broker.process_bar(bar3)
    fsm.on_wave1_break(bar3, 1020, add_volume_steps=2)  # → ADD (平均建値1012)
    fsm.on_structure_event(structure_event(StructureEventType.HL, 1025))  # → SL_AT_BE
    bar4 = mk_candle(4, 1075, 1040, 1070)
    broker.process_bar(bar4)
    fsm.on_half_tp_signal(bar4, Decimal(75))            # RSI>=70 → RUNNER
    assert fsm.state is PosState.RUNNER and fsm.volume_steps == 2
    return fsm


# ---------------------------------------------------------------------------
# プロパティテスト: SL単調性 (CLAUDE.md 第3条)
# ---------------------------------------------------------------------------

@pytest.mark.property
@settings(max_examples=200, deadline=None)
@given(st.lists(
    st.tuples(st.sampled_from(["HL", "LH", "MOVE"]),
              st.integers(min_value=900, max_value=1300)),
    min_size=1, max_size=40,
))
def test_sl_never_retreats_long(ops):
    """どんなランダム操作列でも、ロングのSLは一度も下がらない。

    - HL/LH 構造イベント (価格はランダム)
    - move_sl への直接の引き下げ/引き上げ試行 (引き下げは例外で拒否される)
    """
    broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=0)
    fsm = make_filled_long(broker)
    prev_sl = fsm.sl_int
    assert prev_sl is not None

    for i, (op, price) in enumerate(ops):
        if op == "HL":
            fsm.on_structure_event(structure_event(StructureEventType.HL, price, i))
        elif op == "LH":
            fsm.on_structure_event(structure_event(StructureEventType.LH, price, i))
        else:
            try:
                fsm.move_sl(price)
            except SlMonotonicityError:
                pass
        # ★ 不変条件: SLは利益方向 (上) にしか動かない
        assert fsm.sl_int >= prev_sl
        prev_sl = fsm.sl_int


@pytest.mark.property
@settings(max_examples=200, deadline=None)
@given(st.lists(
    st.tuples(st.sampled_from(["HL", "LH", "MOVE"]),
              st.integers(min_value=900, max_value=1300)),
    min_size=1, max_size=40,
))
def test_sl_never_retreats_after_add_from_sl_at_be(ops):
    """経路B (PROBE→SL_AT_BE→ADD) 後の任意操作列でもSL単調性が保たれる。"""
    broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=0)
    fsm = make_filled_long(broker)
    # SL_AT_BE へ遷移
    fsm.on_structure_event(structure_event(StructureEventType.HL, 1005))
    assert fsm.state is PosState.SL_AT_BE
    # SL_AT_BE から追撃
    bar_add = mk_candle(5, 1045, 1025, 1041)
    broker.process_bar(bar_add)
    assert fsm.on_wave1_break(bar_add, 1020, add_volume_steps=2)
    assert fsm.state is PosState.ADD

    prev_sl = fsm.sl_int
    assert prev_sl is not None
    for i, (op, price) in enumerate(ops):
        if op == "HL":
            fsm.on_structure_event(structure_event(StructureEventType.HL, price, i))
        elif op == "LH":
            fsm.on_structure_event(structure_event(StructureEventType.LH, price, i))
        else:
            try:
                fsm.move_sl(price)
            except SlMonotonicityError:
                pass
        if fsm.state is PosState.CLOSED:
            break
        # ★ 不変条件: SLは利益方向 (上) にしか動かない
        assert fsm.sl_int >= prev_sl
        prev_sl = fsm.sl_int


@pytest.mark.property
@settings(max_examples=100, deadline=None)
@given(st.integers(min_value=0, max_value=10_000))
def test_move_sl_downward_always_rejected(delta):
    """現在SL以下へのあらゆる移動要求は SlMonotonicityError。"""
    broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=0)
    fsm = make_filled_long(broker)
    with pytest.raises(SlMonotonicityError):
        fsm.move_sl(fsm.sl_int - delta)   # 同値 (delta=0) も拒否


@pytest.mark.property
@settings(max_examples=200, deadline=None)
@given(st.lists(st.integers(min_value=0, max_value=100), min_size=1, max_size=30))
def test_half_tp_fires_at_most_once(rsi_seq):
    """設計書 §6.5: 半分利確は高々1回。任意のRSI列を SL_AT_BE 状態へ与えても
    HALF_TAKE_PROFIT は1回以下、かつ残玉は半分を超えて減らない。"""
    broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
    fsm = make_filled_long(broker)
    bar3 = mk_candle(3, 1035, 1015, 1031)
    broker.process_bar(bar3)
    fsm.on_wave1_break(bar3, 1020, add_volume_steps=2)
    fsm.on_structure_event(structure_event(StructureEventType.HL, 1025))
    assert fsm.state is PosState.SL_AT_BE and fsm.volume_steps == 4

    bar = mk_candle(4, 1075, 1040, 1070)
    broker.process_bar(bar)
    for rsi in rsi_seq:
        fsm.on_half_tp_signal(bar, Decimal(rsi))
    # ★ 不変条件: 半分利確は高々1回・残玉は2 (半分) のまま
    assert sum(1 for n, _ in fsm.journal if n == "HALF_TAKE_PROFIT") <= 1
    assert fsm.volume_steps in (2, 4)         # 発火なら2、未発火なら4。0未満や端数は出ない


# ---------------------------------------------------------------------------
# 状態機械ライフサイクル
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_full_happy_path(self):
        broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        fsm = make_filled_long(broker)
        assert fsm.state is PosState.PROBE
        assert (fsm.entry_price_int, fsm.sl_int, fsm.volume_steps) == (990, 960, 2)

        # --- 早計な建値移動は不可能: スイングが建値+10未満では動かない ---
        assert not fsm.on_structure_event(structure_event(StructureEventType.HL, 995))
        assert fsm.sl_int == 960
        # LH (売り用トリガー) はロングでは常に無視
        assert not fsm.on_structure_event(structure_event(StructureEventType.LH, 1100))
        assert fsm.sl_int == 960

        # --- 追撃: ヒゲ/バッファ未満では発火しない (W1高値=1020, buffer=10) ---
        broker.process_bar(mk_candle(2, 1035, 1010, 1025))   # 終値1025 <= 1030
        assert not fsm.on_wave1_break(mk_candle(2, 1035, 1010, 1025), 1020, add_volume_steps=2)
        bar3 = mk_candle(3, 1035, 1015, 1031)                 # 終値1031 > 1030
        broker.process_bar(bar3)
        assert fsm.on_wave1_break(bar3, 1020, add_volume_steps=2)
        assert fsm.state is PosState.ADD and fsm.volume_steps == 4
        # 追撃約定 = 終値1031 + spread2 = 1033。建値は数量加重平均へ更新 (P7):
        #   (990×2 + 1033×2) / 4 = 1011.5 → 銀行家丸め → 1012
        assert fsm.entry_price_int == 1012

        # --- 建値SL: 平均建値基準。安値切り上げ確定 (1025 >= 1012+10) でのみ移動 ---
        assert fsm.on_structure_event(structure_event(StructureEventType.HL, 1025))
        assert fsm.state is PosState.SL_AT_BE
        assert fsm.sl_int == 1014                             # 平均建値1012 + 微益2
        assert broker.position("pos1").sl_int == 1014
        # 再度のHLイベントでは何も起きない (状態ガード)
        assert not fsm.on_structure_event(structure_event(StructureEventType.HL, 1050))

        # --- 半分利確: RSIが利確圏(70)到達で厳密1回 (設計書 §6.4) ---
        bar4 = mk_candle(4, 1075, 1040, 1070)
        broker.process_bar(bar4)
        assert not fsm.on_half_tp_signal(bar4, Decimal(65))   # RSI<70・SR無し → 不発
        assert fsm.state is PosState.SL_AT_BE
        assert fsm.on_half_tp_signal(bar4, Decimal(72))       # RSI>=70 → 半分利確
        assert fsm.state is PosState.RUNNER
        assert fsm.volume_steps == 2                          # 4 → 半分決済
        assert broker.position("pos1").volume_steps == 2
        assert not fsm.on_half_tp_signal(bar4, Decimal(80))   # 2回目は不発 (厳密一回性)

        # --- SLヒットで終了 (建値SL=1014) ---
        events = broker.process_bar(mk_candle(5, 1050, 990, 1000))
        assert [e.kind for e in events] == ["SL_HIT"]
        assert events[0].price_int == 1014
        fsm.on_sl_hit(events[0].price_int)
        assert fsm.state is PosState.CLOSED and fsm.volume_steps == 0
        assert broker.position("pos1") is None

        # ジャーナルに全遷移が記録されている (CLAUDE.md 第11条)
        names = [t for t, _ in fsm.journal]
        assert names == ["PLACE_PROBE", "PROBE_FILL", "ADD_FILL",
                         "MOVE_SL", "SL_TO_BREAKEVEN", "HALF_TAKE_PROFIT", "SL_HIT"]

    def test_be_sl_uses_average_entry_after_add(self):
        """追撃後の建値SLは打診建値ではなく平均建値を基準にする (P7)。

        追撃玉が打診から大きく離れて約定しても、建値SLはポジション全体の
        損益分岐近傍に置かれ、追撃玉だけが大幅マイナスで切られない。
        """
        broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        fsm = make_filled_long(broker)            # entry 990, vol 2, sl 960
        # 大きく上で追撃約定 (W1=1020, buffer=10 → threshold 1030)
        bar = mk_candle(2, 1310, 1290, 1300)      # 終値1300 > 1030
        broker.process_bar(bar)
        assert fsm.on_wave1_break(bar, 1020, add_volume_steps=2)
        # 追撃約定 = 1300 + spread2 = 1302。平均建値 = (990×2 + 1302×2)/4 = 1146
        assert fsm.entry_price_int == 1146
        # 平均建値より十分上のスイングで建値SL移動
        assert fsm.on_structure_event(structure_event(StructureEventType.HL, 1200))
        assert fsm.sl_int == 1148                 # 平均建値1146 + 微益2 (打診建値基準の992ではない)
        assert fsm.sl_int > 990                   # 追撃玉も建値防御に含まれる

    def test_half_tp_via_sr_zone(self):
        """RSIが利確圏でなくても、建値より上の重要RESISTANCEゾーン到達で半分利確。"""
        broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        fsm = make_filled_long(broker)
        bar3 = mk_candle(3, 1035, 1015, 1031)
        broker.process_bar(bar3)
        fsm.on_wave1_break(bar3, 1020, add_volume_steps=2)
        fsm.on_structure_event(structure_event(StructureEventType.HL, 1025))
        assert fsm.state is PosState.SL_AT_BE                  # 平均建値1012
        bar4 = mk_candle(4, 1075, 1040, 1070)
        broker.process_bar(bar4)
        # 建値1012より上のRESISTANCE [1060,1080]。高値1075が到達 → 半分利確
        zone = SRZone(low_int=1060, high_int=1080, touches=2,
                      strength=Decimal("1.8"), role="RESISTANCE")
        assert fsm.on_half_tp_signal(bar4, Decimal(50), (zone,))
        assert fsm.state is PosState.RUNNER
        assert fsm.journal[-1][0] == "HALF_TAKE_PROFIT"
        assert fsm.journal[-1][1]["trigger"] == "SR"

    def test_half_tp_via_sma90_touch(self):
        """RSI/SRが不発でも、確定足が90SMA(建値の利益側)を跨げば半分利確 (③-1)。"""
        broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        fsm = make_filled_long(broker)
        bar3 = mk_candle(3, 1035, 1015, 1031)
        broker.process_bar(bar3)
        fsm.on_wave1_break(bar3, 1020, add_volume_steps=2)
        fsm.on_structure_event(structure_event(StructureEventType.HL, 1025))
        assert fsm.state is PosState.SL_AT_BE                  # 平均建値1012
        bar4 = mk_candle(4, 1075, 1040, 1070)
        broker.process_bar(bar4)
        # 90SMA=1050 は建値1012より上(利益側)。確定足[1040,1075]が跨ぐ → 半分利確
        assert fsm.on_half_tp_signal(bar4, Decimal(50), (), 1050)
        assert fsm.state is PosState.RUNNER
        assert fsm.journal[-1][1]["trigger"] == "SMA90"

    def test_half_tp_sma90_ignored_when_below_entry(self):
        """90SMAが建値の損失側(下)にある接触では発火しない(利確にならない)。"""
        broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        fsm = make_filled_long(broker)
        bar3 = mk_candle(3, 1035, 1015, 1031)
        broker.process_bar(bar3)
        fsm.on_wave1_break(bar3, 1020, add_volume_steps=2)
        fsm.on_structure_event(structure_event(StructureEventType.HL, 1025))  # 建値1012
        bar4 = mk_candle(4, 1015, 1005, 1010)
        broker.process_bar(bar4)
        # 90SMA=1008 は建値1012より下(損失側)。跨いでも不発
        assert not fsm.on_half_tp_signal(bar4, Decimal(50), (), 1008)

    def test_half_tp_ignores_support_and_below_entry_zones(self):
        """買いの半分利確は『建値より上の RESISTANCE 到達』のみ。SUPPORT や
        建値より下のゾーンでは発火しない。"""
        broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        fsm = make_filled_long(broker)
        bar3 = mk_candle(3, 1035, 1015, 1031)
        broker.process_bar(bar3)
        fsm.on_wave1_break(bar3, 1020, add_volume_steps=2)
        fsm.on_structure_event(structure_event(StructureEventType.HL, 1025))
        bar4 = mk_candle(4, 1075, 1040, 1070)
        broker.process_bar(bar4)
        support = SRZone(low_int=1060, high_int=1080, touches=3,
                         strength=Decimal("2.0"), role="SUPPORT")       # role違い
        below = SRZone(low_int=1000, high_int=1010, touches=3,
                       strength=Decimal("2.0"), role="RESISTANCE")      # 建値より下
        assert not fsm.on_half_tp_signal(bar4, Decimal(50), (support, below))
        assert fsm.state is PosState.SL_AT_BE

    def test_runner_closes_at_fib_target(self):
        """残玉決済①: フィボ目標到達で全決済 (設計書 §6.4)。"""
        broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        fsm = make_runner_long(broker)
        bar5 = mk_candle(5, 1070, 1050, 1065)
        broker.process_bar(bar5)
        assert not fsm.on_runner_target(bar5, 1100)        # 高値1070 < 1100 → 未到達
        bar6 = mk_candle(6, 1110, 1060, 1100)
        broker.process_bar(bar6)
        assert fsm.on_runner_target(bar6, 1100)            # 高値1110 >= 1100 → 全決済
        assert fsm.state is PosState.CLOSED and fsm.volume_steps == 0
        assert fsm.journal[-1][0] == "CLOSE_ALL"
        assert fsm.journal[-1][1]["reason"] == "FIB_TARGET"
        assert broker.position("pos1") is None

    def test_runner_closes_on_dow_reversal(self):
        """残玉決済②: ダウ転換確定 (買い→DOWN) で全決済。SUSPECTでは動かない。"""
        broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        fsm = make_runner_long(broker)
        # 転換警戒 (UP_SUSPECT) では決済しない
        suspect = StructureEvent(type=StructureEventType.LH, swing=sw("HIGH", 1100, 9),
                                 prev_swing=sw("HIGH", 1110, 8),
                                 state_after=TrendState.UP_SUSPECT)
        assert not fsm.on_runner_reversal(suspect)
        assert fsm.state is PosState.RUNNER
        # 転換確定 (DOWN) で全決済
        reversal = StructureEvent(type=StructureEventType.LL, swing=sw("LOW", 1000, 11),
                                  prev_swing=sw("LOW", 1010, 10),
                                  state_after=TrendState.DOWN)
        assert fsm.on_runner_reversal(reversal)
        assert fsm.state is PosState.CLOSED
        assert fsm.journal[-1][1]["reason"] == "DOW_REVERSAL"

    def test_runner_exit_only_in_runner_state(self):
        """ランナー出口は RUNNER 状態でのみ作用 (それ以前は無視)。"""
        broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        fsm = make_filled_long(broker)                     # PROBE
        assert not fsm.on_runner_target(mk_candle(2, 1200, 1100, 1150), 1100)
        reversal = StructureEvent(type=StructureEventType.LL, swing=sw("LOW", 1000, 1),
                                  prev_swing=sw("LOW", 1010, 0),
                                  state_after=TrendState.DOWN)
        assert not fsm.on_runner_reversal(reversal)
        assert fsm.state is PosState.PROBE

    def test_pending_expiry_cancels(self):
        broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        broker.process_bar(mk_candle(0, 1005, 995, 1000))
        fsm = PositionFSM(position_id="p", direction=+1, broker=broker, config=CFG)
        expiry = T0 + 3 * Timeframe.M5.duration
        fsm.place_probe(limit_price_int=990, volume_steps=2, sl_int=960,
                        expiry=expiry, invalidation_price=950)
        assert fsm.on_bar_pending(mk_candle(1, 1005, 995, 1000)) is None   # 未失効
        # close_time >= expiry → "expired" を返す (失効リカバリーの判別キー)
        assert fsm.on_bar_pending(mk_candle(3, 1005, 995, 1000)) == "expired"
        assert fsm.state is PosState.CLOSED
        assert broker.pending_count == 0

    def test_pending_invalidation_cancels(self):
        """エリオット無効化価格 (950) を終値が割る → シナリオ破棄で即取消。"""
        broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        broker.process_bar(mk_candle(0, 1005, 995, 1000))
        fsm = PositionFSM(position_id="p", direction=+1, broker=broker, config=CFG)
        fsm.place_probe(limit_price_int=990, volume_steps=2, sl_int=940,
                        expiry=FAR_FUTURE, invalidation_price=950)
        # 無効化は "invalidated" を返す (失効リカバリー対象外の判別キー)
        assert fsm.on_bar_pending(mk_candle(1, 1000, 945, 949)) == "invalidated"
        assert fsm.state is PosState.CLOSED

    def test_pending_invalidation_priority_over_expiry(self):
        """失効と無効化が同時成立 → 無効化が優先 (シナリオ崩壊が支配的)。"""
        broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        broker.process_bar(mk_candle(0, 1005, 995, 1000))
        fsm = PositionFSM(position_id="p", direction=+1, broker=broker, config=CFG)
        expiry = T0 + 1 * Timeframe.M5.duration
        fsm.place_probe(limit_price_int=990, volume_steps=2, sl_int=940,
                        expiry=expiry, invalidation_price=950)
        # close_time >= expiry かつ 終値 949 < invalidation 950 の両立
        assert fsm.on_bar_pending(mk_candle(3, 1000, 945, 949)) == "invalidated"

    def test_closed_bar_required_everywhere(self):
        broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        fsm = make_filled_long(broker)
        forming = mk_candle(2, 1100, 1000, 1090).model_copy(update={"is_closed": False})
        with pytest.raises(ValueError, match="closed bars"):
            fsm.on_wave1_break(forming, 1020, add_volume_steps=2)

    def test_probe_validations(self):
        broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        broker.process_bar(mk_candle(0, 1005, 995, 1000))
        fsm = PositionFSM(position_id="p", direction=+1, broker=broker, config=CFG)
        with pytest.raises(ValueError, match=">= 2 steps"):
            fsm.place_probe(limit_price_int=990, volume_steps=1, sl_int=960,
                            expiry=FAR_FUTURE, invalidation_price=950)
        with pytest.raises(ValueError, match="losing side"):
            fsm.place_probe(limit_price_int=990, volume_steps=2, sl_int=995,
                            expiry=FAR_FUTURE, invalidation_price=950)

    def test_invalid_transitions_raise(self):
        broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        broker.process_bar(mk_candle(0, 1005, 995, 1000))
        fsm = PositionFSM(position_id="p", direction=+1, broker=broker, config=CFG)
        with pytest.raises(TransitionError):
            fsm.on_probe_fill(990, 2)            # IDLE では約定通知を受けられない
        with pytest.raises(TransitionError):
            fsm.on_wave1_break(mk_candle(1, 1050, 1000, 1040), 1020, add_volume_steps=2)

    def test_add_from_sl_at_be_path(self):
        """経路B: PROBE → SL_AT_BE → ADD → SL_AT_BE の追撃 (手法の心理的優位性)。

        建値SL移動 (HL確定) 後、さらに価格が第1波高値を突破した場合に
        SL_AT_BE 状態から追撃が発動し、新しい平均建値を基準に SL が再設定される。
        """
        broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        fsm = make_filled_long(broker)           # PROBE: entry=990, sl=960, vol=2

        # HL確定で PROBE → SL_AT_BE (1005 >= 990+min_be=10 → OK)
        assert fsm.on_structure_event(structure_event(StructureEventType.HL, 1005))
        assert fsm.state is PosState.SL_AT_BE
        assert fsm.sl_int == 992                 # 990 + be_offset=2

        # W1(=1020)+buffer(=10)=1030 を超える足 → SL_AT_BE から追撃発動
        bar = mk_candle(5, 1045, 1025, 1041)     # 終値1041 > 1030
        broker.process_bar(bar)
        assert fsm.on_wave1_break(bar, 1020, add_volume_steps=2)
        assert fsm.state is PosState.ADD
        assert fsm.volume_steps == 4
        # 追撃約定 = 1041+spread2=1043, avg = (990×2+1043×2)/4 = 4066/4 = 1016.5 → 1016
        assert fsm.entry_price_int == 1016

        # 次のHL確定で ADD → SL_AT_BE。SLは新しい平均建値ベースへ更新
        # 1030 >= 1016 + min_be=10 → OK。既存SL=992 < new_sl=1018 → 単調性 ✓
        assert fsm.on_structure_event(structure_event(StructureEventType.HL, 1030))
        assert fsm.state is PosState.SL_AT_BE
        assert fsm.sl_int == 1018                # 平均建値1016 + be_offset=2

        # さらに W1ブレイク条件が続いても追撃は起きない (_add_fired=True)
        bar2 = mk_candle(6, 1055, 1030, 1052)
        broker.process_bar(bar2)
        assert not fsm.on_wave1_break(bar2, 1020, add_volume_steps=2)
        assert fsm.volume_steps == 4

        # 半分利確: RSI>=70 → RUNNER
        bar3 = mk_candle(7, 1080, 1045, 1075)
        broker.process_bar(bar3)
        assert fsm.on_half_tp_signal(bar3, Decimal(72))
        assert fsm.state is PosState.RUNNER
        assert fsm.volume_steps == 2

        # ジャーナル: 経路B の全遷移が記録されている
        names = [t for t, _ in fsm.journal]
        assert names == ["PLACE_PROBE", "PROBE_FILL",
                         "MOVE_SL", "SL_TO_BREAKEVEN",     # 1st HL → SL_AT_BE
                         "ADD_FILL",                        # W1ブレイク → ADD
                         "MOVE_SL", "SL_TO_BREAKEVEN",     # 2nd HL → SL_AT_BE
                         "HALF_TAKE_PROFIT"]

    def test_add_not_fired_twice_via_probe_then_sl_at_be(self):
        """経路A経由後に SL_AT_BE へ戻っても追撃は2回目を発動しない。"""
        broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        fsm = make_filled_long(broker)
        # 経路A: PROBE → ADD
        bar3 = mk_candle(3, 1035, 1015, 1031)
        broker.process_bar(bar3)
        assert fsm.on_wave1_break(bar3, 1020, add_volume_steps=2)
        assert fsm.state is PosState.ADD
        # ADD → SL_AT_BE
        fsm.on_structure_event(structure_event(StructureEventType.HL, 1025))
        assert fsm.state is PosState.SL_AT_BE
        # SL_AT_BE で再度 W1ブレイク条件 → 2回目は発動しない
        bar4 = mk_candle(4, 1060, 1035, 1055)
        broker.process_bar(bar4)
        assert not fsm.on_wave1_break(bar4, 1020, add_volume_steps=2)
        assert fsm.volume_steps == 4             # 変化なし (ADD済みの4のまま)


# ---------------------------------------------------------------------------
# Simブローカー
# ---------------------------------------------------------------------------

class TestSimBroker:
    def test_idempotent_limit(self):
        b = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        for _ in range(3):   # 同一IDの再発注は無視される
            b.place_limit(client_order_id="oid1", position_id="p", direction=+1,
                          price_int=990, volume_steps=2, sl_int=960)
        assert b.pending_count == 1

    def test_idempotent_market(self):
        b = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        b.process_bar(mk_candle(0, 1005, 995, 1000))
        b.place_market(client_order_id="m1", position_id="p", direction=+1,
                       volume_steps=2, sl_int=960)
        b.place_market(client_order_id="m1", position_id="p", direction=+1,
                       volume_steps=2, sl_int=960)
        assert b.position("p").volume_steps == 2   # 二重建てしない

    def test_min_stop_distance_rejected(self):
        b = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        with pytest.raises(BrokerRejection, match="stop too close"):
            b.place_limit(client_order_id="o", position_id="p", direction=+1,
                          price_int=990, volume_steps=2, sl_int=988)

    def test_limit_fill_respects_spread(self):
        """買い指値990: ask最安値 = l + spread が990以下のときだけ約定。"""
        b = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        b.place_limit(client_order_id="o", position_id="p", direction=+1,
                      price_int=990, volume_steps=2, sl_int=960)
        assert b.process_bar(mk_candle(0, 1000, 989, 995)) == []      # 989+2=991 > 990
        events = b.process_bar(mk_candle(1, 1000, 988, 995))          # 988+2=990 <= 990
        assert [e.kind for e in events] == ["FILL"]
        assert events[0].price_int == 990

    def test_market_fill_at_ask(self):
        b = SimBroker(spread_ticks=3, min_stop_distance_ticks=5)
        b.process_bar(mk_candle(0, 1005, 995, 1000))
        fill = b.place_market(client_order_id="m", position_id="p", direction=+1,
                              volume_steps=2, sl_int=960)
        assert fill == 1003                       # last_close 1000 + spread 3

    def test_forming_bar_rejected(self):
        b = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        forming = mk_candle(0, 1005, 995, 1000).model_copy(update={"is_closed": False})
        with pytest.raises(ValueError, match="closed candles only"):
            b.process_bar(forming)


# ---------------------------------------------------------------------------
# リスクマネージャー (独立拒否権)
# ---------------------------------------------------------------------------

RISK_CFG = RiskConfig(max_position_volume_steps=4, max_total_volume_steps=6,
                      max_spread_ticks=10, daily_loss_limit_tick_steps=1000)


def req(volume: int = 2) -> OrderRequest:
    return OrderRequest(symbol="XAUUSD", direction=+1, volume_steps=volume, kind="PROBE_LIMIT")


class TestRiskManager:
    def test_normal_approval(self):
        rm = RiskManager(RISK_CFG)
        v = rm.approve(req(), current_spread_ticks=3, open_total_volume_steps=0)
        assert v and v.reason == "OK"

    def test_position_volume_cap(self):
        rm = RiskManager(RISK_CFG)
        v = rm.approve(req(volume=5), current_spread_ticks=3, open_total_volume_steps=0)
        assert not v and "POSITION_VOLUME_CAP" in v.reason

    def test_total_volume_cap(self):
        rm = RiskManager(RISK_CFG)
        v = rm.approve(req(volume=3), current_spread_ticks=3, open_total_volume_steps=4)
        assert not v and "TOTAL_VOLUME_CAP" in v.reason

    def test_abnormal_spread_rejected(self):
        """指標スパイク時 (スプレッド異常) はどんなシグナルでも新規拒否。"""
        rm = RiskManager(RISK_CFG)
        v = rm.approve(req(), current_spread_ticks=25, open_total_volume_steps=0)
        assert not v and "SPREAD_ABNORMAL" in v.reason

    def test_daily_loss_latches_kill_switch(self):
        rm = RiskManager(RISK_CFG)
        rm.record_realized(-400)
        assert not rm.kill_switch_engaged
        rm.record_realized(-600)                  # 累計 -1000 で作動
        assert rm.kill_switch_engaged
        v = rm.approve(req(), current_spread_ticks=1, open_total_volume_steps=0)
        assert not v and "KILL_SWITCH" in v.reason
        # 日付が変わってもラッチは維持 (明示リセットまで全拒否)
        rm.new_day()
        assert rm.kill_switch_engaged
        assert rm.daily_realized_tick_steps == 0
        rm.reset_kill_switch()
        assert rm.approve(req(), current_spread_ticks=1, open_total_volume_steps=0)

    def test_invalid_config_rejected(self):
        with pytest.raises(ValueError):
            RiskConfig(max_position_volume_steps=0, max_total_volume_steps=6,
                       max_spread_ticks=10, daily_loss_limit_tick_steps=1000)


# ---------------------------------------------------------------------------
# VolumeSizer — 可変ロットサイジング
# ---------------------------------------------------------------------------

class TestVolumeSizer:
    from infers.execution.risk import VolumeSizer, VolumeSizerConfig

    TICK_VALUE = Decimal("0.01")  # XAUUSD: 0.01 USD/tick/step

    def _sizer(self, risk_pct="0.01", min_steps=2, max_steps=100):
        from infers.execution.risk import VolumeSizer, VolumeSizerConfig
        return VolumeSizer(VolumeSizerConfig(
            risk_pct=Decimal(risk_pct),
            tick_value_per_step=self.TICK_VALUE,
            min_volume_steps=min_steps,
            max_volume_steps=max_steps,
        ))

    def test_basic_sizing(self):
        """equity=$10000, risk=1%, SL=300tick($3.00) → 33 steps (=0.33lot)"""
        sizer = self._sizer()
        steps = sizer.calc_volume_steps(Decimal("10000"), sl_distance_ticks=300)
        # floor(10000 × 0.01 / (300 × 0.01)) = floor(100 / 3) = 33
        assert steps == 33

    def test_equity_doubles_steps_double(self):
        """残高が2倍になればロットも2倍になる (比例性)。"""
        sizer = self._sizer()
        # SL=500tick → s1=floor(100/5)=20, s2=floor(200/5)=40 (max=100に当たらない)
        s1 = sizer.calc_volume_steps(Decimal("10000"), sl_distance_ticks=500)
        s2 = sizer.calc_volume_steps(Decimal("20000"), sl_distance_ticks=500)
        assert s2 == s1 * 2

    def test_min_volume_floor(self):
        """残高ゼロでも min_volume_steps (=2) を返す (FSM半分利確の最低要件)。"""
        sizer = self._sizer()
        assert sizer.calc_volume_steps(Decimal("0"), sl_distance_ticks=300) == 2

    def test_sl_zero_returns_min(self):
        """SL距離ゼロは min_volume_steps (=2) を返す (ゼロ除算防止)。"""
        sizer = self._sizer()
        assert sizer.calc_volume_steps(Decimal("10000"), sl_distance_ticks=0) == 2

    def test_max_volume_clip(self):
        """巨大残高でも max_volume_steps でクリップされる。"""
        sizer = self._sizer(max_steps=5)
        steps = sizer.calc_volume_steps(Decimal("10_000_000"), sl_distance_ticks=1)
        assert steps == 5

    def test_wide_sl_reduces_steps(self):
        """SL距離が大きいほどロットが減る (リスク一定)。"""
        sizer = self._sizer()
        s_narrow = sizer.calc_volume_steps(Decimal("10000"), sl_distance_ticks=100)
        s_wide   = sizer.calc_volume_steps(Decimal("10000"), sl_distance_ticks=500)
        assert s_narrow > s_wide

    def test_integer_floor(self):
        """端数は切り捨て (切り上げるとリスク超過になる)。"""
        sizer = self._sizer()
        # floor(100 / 3.00) = 33.33... → 33
        steps = sizer.calc_volume_steps(Decimal("10000"), sl_distance_ticks=300)
        risk = steps * 300 * self.TICK_VALUE
        assert risk <= Decimal("10000") * Decimal("0.01")

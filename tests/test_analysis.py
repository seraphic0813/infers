"""分析層の単体テスト: インジケーター・ZigZag・ダウFSM。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from infers.analysis.dow import DowStateMachine, StructureEventType, TrendState
from infers.analysis.indicators import ATR, Q, SMA, RsiState, WilderRSI, rsi_forward
from infers.analysis.zigzag import SwingPoint, ZigZagDetector
from infers.data.models import Candle, Timeframe

UTC = timezone.utc
T0 = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


def mk_candle(i: int, h: int, l: int, c: int | None = None, tf: Timeframe = Timeframe.M5) -> Candle:
    """テスト用確定足。i はバー番号(時刻を単調増加させる)。"""
    close = c if c is not None else (h + l) // 2
    return Candle(
        symbol="XAUUSD", tf=tf,
        open_time=T0 + i * tf.duration,
        o_int=(h + l) // 2, h_int=h, l_int=l, c_int=close,
        volume=1, is_closed=True,
    )


# ---------------------------------------------------------------------------
# SMA / ATR
# ---------------------------------------------------------------------------

class TestSMA:
    def test_warmup_returns_none(self):
        sma = SMA(3)
        assert sma.update(10) is None
        assert sma.update(20) is None
        assert not sma.is_ready

    def test_rolling_value(self):
        sma = SMA(3)
        sma.update(10); sma.update(20)
        assert sma.update(30) == Decimal(20)
        assert sma.update(40) == Decimal(30)   # (20+30+40)/3

    def test_exact_quantization(self):
        sma = SMA(3)
        sma.update(1); sma.update(1)
        assert sma.update(2) == Decimal("1.333333333")  # 4/3 を 1e-9 量子化


class TestATR:
    def test_seed_then_wilder(self):
        atr = ATR(3)
        assert atr.update(10, 5, 8) is None            # TR=5 (初回は h-l)
        assert atr.update(12, 7, 9) is None            # TR=max(5,4,1)=5
        seed = atr.update(15, 9, 14)                   # TR=max(6,6,0)=6 → (5+5+6)/3
        assert seed == (Decimal(16) / 3).quantize(Q)
        nxt = atr.update(20, 14, 18)                   # TR=max(6,6,0)=6 → Wilder
        assert nxt == ((seed * 2 + 6) / 3).quantize(Q)

    def test_invalid_hl_rejected(self):
        with pytest.raises(ValueError):
            ATR(3).update(5, 10, 7)


# ---------------------------------------------------------------------------
# RSI (Wilder) — 内部状態の検証
# ---------------------------------------------------------------------------

class TestWilderRSI:
    def test_seed_state(self):
        rsi = WilderRSI(period=3)
        for c in (100, 110, 105):       # deltas: +10, -5 (まだ2個)
            assert rsi.update(c) is None
        val = rsi.update(115)           # delta +10 で3個 → シード完了
        st = rsi.state
        assert st is not None
        assert st.avg_gain == (Decimal(20) / 3).quantize(Q)
        assert st.avg_loss == (Decimal(5) / 3).quantize(Q)
        assert st.last_close_int == 115
        assert val == pytest.approx(Decimal(80), abs=Decimal("0.001"))

    def test_wilder_update_law(self):
        rsi = WilderRSI(period=3)
        for c in (100, 110, 105, 115):
            rsi.update(c)
        prev = rsi.state
        rsi.update(112)                 # loss 3
        st = rsi.state
        assert st.avg_gain == ((prev.avg_gain * 2 + 0) / 3).quantize(Q)
        assert st.avg_loss == ((prev.avg_loss * 2 + 3) / 3).quantize(Q)
        assert st.last_close_int == 112

    def test_all_gains_rsi_100(self):
        rsi = WilderRSI(period=3)
        for c in (100, 101, 102):
            rsi.update(c)
        assert rsi.update(103) == Decimal(100)

    def test_flat_rsi_50(self):
        st = RsiState(period=3, avg_gain=Decimal(0), avg_loss=Decimal(0), last_close_int=100)
        assert st.rsi == Decimal(50)

    def test_rsi_forward_is_pure_and_consistent(self):
        """純粋関数の前進計算がステートフル更新と完全一致し、元stateを変更しない。"""
        rsi = WilderRSI(period=3)
        for c in (100, 110, 105, 115):
            rsi.update(c)
        snapshot = rsi.state
        path = [112, 108, 120]

        # 純粋関数で前進
        fwd_rsi, fwd_state = rsi_forward(snapshot, path)
        # snapshot は不変 (frozen + 非破壊)
        assert snapshot.last_close_int == 115

        # ステートフル更新と一致 (同一コードパスの保証)
        for c in path:
            stateful_rsi = rsi.update(c)
        assert fwd_rsi == stateful_rsi
        assert fwd_state == rsi.state


# ---------------------------------------------------------------------------
# ZigZag — 確定遅延つきスイング検出
# ---------------------------------------------------------------------------

class TestZigZag:
    def test_rejects_forming_bar(self):
        zz = ZigZagDetector(reversal_ticks=50)
        forming = mk_candle(0, 1000, 990).model_copy(update={"is_closed": False})
        with pytest.raises(ValueError, match="closed candles only"):
            zz.update(forming)

    def test_rejects_non_monotonic_time(self):
        zz = ZigZagDetector(reversal_ticks=50)
        zz.update(mk_candle(1, 1000, 990))
        with pytest.raises(ValueError, match="strictly increasing"):
            zz.update(mk_candle(1, 1010, 1000))

    def test_swing_detection_with_confirmation_delay(self):
        """設計書 §3.1: 極値は反転閾値到達バーのクローズで遅れて確定する。"""
        zz = ZigZagDetector(reversal_ticks=50)

        assert zz.update(mk_candle(1, 1000, 990)) is None    # 初期化
        assert zz.update(mk_candle(2, 1010, 1000)) is None   # どちらも閾値未達

        # bar3: 高値1050。安値候補990(bar1)から +60 ≥ 50 → 最初のLOW確定
        low = zz.update(mk_candle(3, 1050, 1040))
        assert isinstance(low, SwingPoint)
        assert low.kind == "LOW"
        assert low.price_int == 990
        assert low.bar_time == T0 + 1 * Timeframe.M5.duration       # 極値はbar1
        assert low.confirmed_at == mk_candle(3, 1050, 1040).close_time  # 確定はbar3クローズ(遅延)

        assert zz.update(mk_candle(4, 1060, 1050)) is None   # 高値候補更新のみ(1060)

        # bar5: 安値1000。高値候補1060(bar4)から -60 ≥ 50 → HIGH確定
        high = zz.update(mk_candle(5, 1055, 1000))
        assert high is not None
        assert high.kind == "HIGH"
        assert high.price_int == 1060
        assert high.bar_time == T0 + 4 * Timeframe.M5.duration
        assert high.confirmed_at == mk_candle(5, 1055, 1000).close_time

        # 以後は安値候補を追跡: bar6で995へ更新、bar7の高値1050で確定 (1050-995=55≥50)
        assert zz.update(mk_candle(6, 1010, 995)) is None
        low2 = zz.update(mk_candle(7, 1050, 1045))
        assert low2 is not None and low2.kind == "LOW" and low2.price_int == 995

    def test_noise_below_threshold_ignored(self):
        """閾値未満の振動ではスイングが確定しない(ノイズ除去)。"""
        zz = ZigZagDetector(reversal_ticks=100)
        for i in range(1, 20):
            h = 1000 + (i % 3) * 20   # ±40程度の振動
            assert zz.update(mk_candle(i, h, h - 30)) is None


# ---------------------------------------------------------------------------
# ダウ理論FSM
# ---------------------------------------------------------------------------

def mk_swing(kind: str, price: int, i: int) -> SwingPoint:
    t = T0 + i * Timeframe.H1.duration
    return SwingPoint(kind=kind, bar_time=t, price_int=price, tf=Timeframe.H1,
                      confirmed_at=t + Timeframe.H1.duration)


class TestDowStateMachine:
    def test_uptrend_establish_then_reversal(self):
        """マニュアル2.1: HH+HL でUP、LH+LL の両成立で転換確定。"""
        fsm = DowStateMachine()

        assert fsm.on_swing(mk_swing("LOW", 990, 1)) is None     # 先行同種なし
        assert fsm.on_swing(mk_swing("HIGH", 1060, 2)) is None
        assert fsm.state == TrendState.UNDEFINED

        ev = fsm.on_swing(mk_swing("LOW", 995, 3))               # 安値切り上げ
        assert ev.type == StructureEventType.HL
        assert fsm.state == TrendState.UNDEFINED                 # まだペア未成立

        ev = fsm.on_swing(mk_swing("HIGH", 1100, 4))             # 高値更新
        assert ev.type == StructureEventType.HH
        assert fsm.state == TrendState.UP                        # HH+HL → UP確定

        ev = fsm.on_swing(mk_swing("HIGH", 1080, 6))             # 高値更新失敗
        assert ev.type == StructureEventType.LH
        assert fsm.state == TrendState.UP_SUSPECT                # 警戒(まだ転換ではない)

        ev = fsm.on_swing(mk_swing("LOW", 940, 7))               # 安値切り下げ
        assert ev.type == StructureEventType.LL
        assert fsm.state == TrendState.DOWN                      # LH+LL → 転換確定

    def test_suspect_cleared_by_hh(self):
        """警戒中でも高値更新(HH)で上昇継続にリセットされる。"""
        fsm = DowStateMachine()
        fsm.on_swing(mk_swing("LOW", 990, 1))
        fsm.on_swing(mk_swing("HIGH", 1060, 2))
        fsm.on_swing(mk_swing("LOW", 995, 3))
        fsm.on_swing(mk_swing("HIGH", 1100, 4))
        fsm.on_swing(mk_swing("HIGH", 1080, 6))                  # LH → SUSPECT
        assert fsm.state == TrendState.UP_SUSPECT
        ev = fsm.on_swing(mk_swing("HIGH", 1150, 8))             # HH → 警戒解除
        assert ev.type == StructureEventType.HH
        assert fsm.state == TrendState.UP

    def test_equal_high_counts_as_failure(self):
        """同値の高値は「更新失敗(LH)」とみなす。"""
        fsm = DowStateMachine()
        fsm.on_swing(mk_swing("HIGH", 1100, 1))
        ev = fsm.on_swing(mk_swing("HIGH", 1100, 3))
        assert ev.type == StructureEventType.LH

    def test_event_carries_state_and_prev(self):
        fsm = DowStateMachine()
        fsm.on_swing(mk_swing("LOW", 990, 1))
        ev = fsm.on_swing(mk_swing("LOW", 1000, 2))
        assert ev.prev_swing.price_int == 990
        assert ev.state_after == fsm.state

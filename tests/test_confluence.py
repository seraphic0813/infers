"""ミクロ分析・レジサポ・コンフルエンス統合の単体テスト。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from infers.analysis.confluence import ConfluenceCluster, Evidence, Family, find_clusters
from infers.analysis.micro import (
    GranvilleDetector, RsiExtremeDetector, RsiExtremeRecency, classify_rsi,
    normalized_deviation, sma_slope,
)
from infers.analysis.support_resistance import build_zones
from infers.analysis.zigzag import SwingPoint
from infers.data.models import Candle, Timeframe

UTC = timezone.utc
T0 = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


def mk_candle(i: int, o: int, h: int, l: int, c: int) -> Candle:
    return Candle(symbol="XAUUSD", tf=Timeframe.H1,
                  open_time=T0 + i * Timeframe.H1.duration,
                  o_int=o, h_int=h, l_int=l, c_int=c, volume=1, is_closed=True)


def sw(kind: str, price: int, i: int) -> SwingPoint:
    t = T0 + i * Timeframe.H1.duration
    return SwingPoint(kind=kind, bar_time=t, price_int=price, tf=Timeframe.H1,
                      confirmed_at=t + Timeframe.H1.duration)


def ev(family: Family, direction: int, tf: Timeframe, zone: tuple[int, int],
       weight: str = "1", source: str = "", valid_until: datetime | None = None) -> Evidence:
    return Evidence(family=family, source=source or family.value, direction=direction,
                    tf=tf, zone=zone, weight=Decimal(weight), valid_until=valid_until)


# ---------------------------------------------------------------------------
# ミクロ分析 (グランビル / RSI)
# ---------------------------------------------------------------------------

class TestNormalization:
    def test_deviation(self):
        # (1035 - 1000) / 10 = 3.5
        assert normalized_deviation(1035, Decimal(1000), Decimal(10)) == Decimal("3.5")

    def test_slope(self):
        assert sma_slope(Decimal(1010), Decimal(1000), Decimal(10), 5) == Decimal("0.2")

    def test_zero_atr_rejected(self):
        with pytest.raises(ValueError):
            normalized_deviation(1000, Decimal(1000), Decimal(0))


class TestGranville:
    SMA = Decimal(1000)
    ATR = Decimal(10)

    def test_rejects_forming_bar(self):
        det = GranvilleDetector(90)
        forming = mk_candle(0, 1000, 1010, 990, 1005).model_copy(update={"is_closed": False})
        with pytest.raises(ValueError, match="closed candles only"):
            det.update(forming, self.SMA, self.ATR, Decimal("0.1"))

    def test_buy3_sma_bounce(self):
        """買③: 安値がSMAタッチ圏 (d_low=-0.2)、終値はSMA上、slope>=0。"""
        det = GranvilleDetector(90)
        sigs = det.update(mk_candle(0, 1003, 1008, 998, 1005), self.SMA, self.ATR, Decimal("0.1"))
        assert [s.kind for s in sigs] == ["BUY3"]
        assert sigs[0].direction == +1 and sigs[0].sma_period == 90

    def test_buy3_blocked_by_negative_slope(self):
        det = GranvilleDetector(90)
        sigs = det.update(mk_candle(0, 1003, 1008, 998, 1005), self.SMA, self.ATR, Decimal("-0.1"))
        assert sigs == []

    def test_buy3_blocked_by_deep_pierce(self):
        """θ_pierce=0.8 を超える割り込み (d_low=-1.5) は買③不成立。"""
        det = GranvilleDetector(90)
        sigs = det.update(mk_candle(0, 1003, 1008, 985, 1005), self.SMA, self.ATR, Decimal("0.1"))
        assert sigs == []

    def test_buy2_return_from_above(self):
        """買②: 上方乖離の履歴 → SMA接近 + 陽線反転で成立。"""
        det = GranvilleDetector(90)
        det.update(mk_candle(0, 1018, 1022, 1015, 1020), self.SMA, self.ATR, Decimal("0.1"))  # d=2.0 (履歴)
        det.update(mk_candle(1, 1010, 1012, 1004, 1006), self.SMA, self.ATR, Decimal("0.1"))  # 接近中
        # 終値1002 (|d|=0.2), 陽線かつ前バー高値1012を…超えない → まず不成立を確認
        sigs = det.update(mk_candle(2, 1000, 1011, 999, 1002), self.SMA, self.ATR, Decimal("0.1"))
        assert "BUY2" not in [s.kind for s in sigs]
        # 陽線反転 (終値が前バー高値超え) かつ |d|<=0.3 → 成立
        det2 = GranvilleDetector(90)
        det2.update(mk_candle(0, 1018, 1022, 1015, 1020), self.SMA, self.ATR, Decimal("0.1"))
        det2.update(mk_candle(1, 1000, 1001, 996, 998), self.SMA, self.ATR, Decimal("0.1"))
        sigs = det2.update(mk_candle(2, 998, 1003, 997, 1003), self.SMA, self.ATR, Decimal("0.1"))
        assert "BUY2" in [s.kind for s in sigs]

    def test_buy4_far_deviation_reversal(self):
        """買④: d<=-3.0 + 陽線反転。slope の符号に依存しない。"""
        det = GranvilleDetector(90)
        det.update(mk_candle(0, 962, 964, 956, 958), self.SMA, self.ATR, Decimal("-0.2"))
        sigs = det.update(mk_candle(1, 958, 972, 955, 970), self.SMA, self.ATR, Decimal("-0.2"))
        assert [s.kind for s in sigs] == ["BUY4"]
        assert sigs[0].d_close == Decimal("-3.0")

    def test_sell3_mirror(self):
        """売③: 高値がSMAタッチ圏、終値はSMA下、slope<=0。"""
        det = GranvilleDetector(90)
        sigs = det.update(mk_candle(0, 997, 1002, 992, 995), self.SMA, self.ATR, Decimal("-0.1"))
        assert [s.kind for s in sigs] == ["SELL3"]
        assert sigs[0].direction == -1


class TestRsiExtreme:
    def test_classify(self):
        assert classify_rsi(Decimal(25)) == "OVERSOLD"
        assert classify_rsi(Decimal(30)) == "OVERSOLD"
        assert classify_rsi(Decimal(75)) == "OVERBOUGHT"
        assert classify_rsi(Decimal(50)) is None

    def test_cross_in_fires_once(self):
        det = RsiExtremeDetector()
        assert det.update(Decimal(40)) is None
        assert det.update(Decimal(28)) == "OVERSOLD"   # 突入で発火
        assert det.update(Decimal(25)) is None         # 圏内継続では再発火しない
        assert det.update(Decimal(35)) is None          # 圏外へ
        assert det.update(Decimal(29)) == "OVERSOLD"   # 再突入で再発火


class TestRsiExtremeRecency:
    """G2-⑤ ②③④「突入イベント化 + 有効時間窓」の判定 (上位足RSI)。"""

    def test_active_on_crossin(self):
        rec = RsiExtremeRecency(window=3)
        rec.update(Decimal(28))                     # 売られすぎ突入
        assert rec.active(+1) is True               # 買い方向で有効
        assert rec.active(-1) is False              # 売り方向では無効

    def test_window_holds_after_bounce(self):
        """突入後に圏外へ反発/反落しても、窓内 (突入足+window本) は有効。"""
        rec = RsiExtremeRecency(window=3)
        rec.update(Decimal(28))                     # 突入 (rem=4)
        rec.update(Decimal(33))                     # 反発1本目 (rem=3)
        rec.update(Decimal(36))                     # 2本目 (rem=2)
        rec.update(Decimal(40))                     # 3本目 (rem=1)
        assert rec.active(+1) is True               # 突入足含め4本目まで有効
        rec.update(Decimal(45))                     # 4本目 (rem=0)
        assert rec.active(+1) is False              # 窓を超えて失効

    def test_betatuki_is_single_event(self):
        """ベタ付き (圏内に長期滞在) でも単一イベント: 窓を過ぎれば圏内でも失効。"""
        rec = RsiExtremeRecency(window=2)
        rec.update(Decimal(28))                     # 突入 (rem=3)
        rec.update(Decimal(25))                     # 滞在・再点火しない (rem=2)
        rec.update(Decimal(27))                     # 滞在 (rem=1)
        assert rec.active(+1) is True
        rec.update(Decimal(26))                     # 滞在 (rem=0)
        assert rec.active(+1) is False              # 圏内でも窓切れで失効 (スパム防止)

    def test_reentry_restarts_window(self):
        """いったん圏外へ出て再突入すると窓が再点火する。"""
        rec = RsiExtremeRecency(window=1)
        rec.update(Decimal(28))                     # 突入 (rem=2)
        rec.update(Decimal(40))                     # 圏外 (rem=1)
        rec.update(Decimal(45))                     # 圏外 (rem=0)
        assert rec.active(+1) is False
        rec.update(Decimal(29))                     # 再突入 (rem=2)
        assert rec.active(+1) is True

    def test_never_reached_is_inactive(self):
        rec = RsiExtremeRecency(window=3)
        rec.update(Decimal(50))
        rec.update(Decimal(45))
        assert rec.active(+1) is False
        assert rec.active(-1) is False

    def test_window_zero_is_crossin_only(self):
        """window=0 は突入した足のみ有効。"""
        rec = RsiExtremeRecency(window=0)
        rec.update(Decimal(28))
        assert rec.active(+1) is True
        rec.update(Decimal(25))                     # 滞在しても翌足で失効
        assert rec.active(+1) is False

    def test_overbought_direction(self):
        rec = RsiExtremeRecency(window=2)
        rec.update(Decimal(72))                     # 買われすぎ突入
        assert rec.active(-1) is True               # 売り方向で有効
        assert rec.active(+1) is False


# ---------------------------------------------------------------------------
# レジサポゾーン
# ---------------------------------------------------------------------------

class TestSRZones:
    def test_clustering_and_roles(self):
        """近接スイング(1000,1003)は1ゾーンに併合、1500は別ゾーン。"""
        swings = [sw("LOW", 1000, 0), sw("LOW", 1003, 2), sw("HIGH", 1500, 4)]
        zones = build_zones(swings, atr=Decimal(10), ref_price_int=1200)
        assert len(zones) == 2
        support = next(z for z in zones if z.role == "SUPPORT")
        resistance = next(z for z in zones if z.role == "RESISTANCE")
        assert support.touches == 2
        assert support.contains(1000) and support.contains(1003)
        assert resistance.touches == 1 and resistance.contains(1500)

    def test_min_zone_width(self):
        """単独スイングでも ε 以上の帯幅が保証される (点ではなく帯)。"""
        zones = build_zones([sw("HIGH", 1500, 0)], atr=Decimal(10), ref_price_int=1000)
        z = zones[0]
        assert z.high_int - z.low_int >= 5   # ε = 0.5 × 10 = 5

    def test_recency_weighted_strength(self):
        """同タッチ数なら新しいタッチを含むゾーンの方が強い。"""
        swings = [sw("LOW", 1000, 0), sw("HIGH", 1500, 2)]  # 1500の方が新しい
        zones = build_zones(swings, atr=Decimal(10), ref_price_int=1200)
        newer = next(z for z in zones if z.contains(1500))
        older = next(z for z in zones if z.contains(1000))
        assert newer.strength > older.strength

    def test_empty_input(self):
        assert build_zones([], atr=Decimal(10), ref_price_int=1000) == []


# ---------------------------------------------------------------------------
# コンフルエンス統合
# ---------------------------------------------------------------------------

class TestConfluence:
    def test_two_families_overlapping_cluster(self):
        evidences = [
            ev(Family.RSI, +1, Timeframe.M5, (990, 1010)),
            ev(Family.GRANVILLE, +1, Timeframe.H1, (995, 1015)),
        ]
        clusters = find_clusters(evidences)
        assert len(clusters) == 1
        c = clusters[0]
        assert c.distinct_families == 2
        assert c.zone == (995, 1010)            # 交差区間
        assert c.direction == +1
        # スコア = RSI(M5: 1×1.0) + GRANVILLE(H1: 1×1.5) = 2.5
        assert c.score == Decimal("2.5")

    def test_same_family_only_is_rejected(self):
        """M5のRSI30とM15のRSI30は同一family=1根拠 → クラスタ不成立。"""
        evidences = [
            ev(Family.RSI, +1, Timeframe.M5, (990, 1010)),
            ev(Family.RSI, +1, Timeframe.M15, (995, 1015)),
        ]
        assert find_clusters(evidences) == []

    def test_same_family_duplicate_does_not_inflate_score(self):
        """同一familyの重複はスコアに最強1件のみ寄与する。"""
        evidences = [
            ev(Family.RSI, +1, Timeframe.M5, (990, 1010)),     # 1.0
            ev(Family.RSI, +1, Timeframe.M15, (992, 1012)),    # 1.2 ← RSI代表
            ev(Family.GRANVILLE, +1, Timeframe.H1, (995, 1015)),  # 1.5
        ]
        clusters = find_clusters(evidences)
        assert len(clusters) == 1
        assert clusters[0].distinct_families == 2
        assert clusters[0].score == Decimal("2.7")   # 1.2 + 1.5 (1.0は重複で不採用)

    def test_non_overlapping_zones_do_not_cluster(self):
        evidences = [
            ev(Family.RSI, +1, Timeframe.M5, (990, 1000)),
            ev(Family.GRANVILLE, +1, Timeframe.H1, (1050, 1060)),
        ]
        assert find_clusters(evidences) == []

    def test_directions_never_mix(self):
        """買い根拠と売り根拠は同一ゾーンでも混在しない。"""
        evidences = [
            ev(Family.RSI, +1, Timeframe.M5, (990, 1010)),
            ev(Family.SR, -1, Timeframe.H1, (995, 1015)),
        ]
        assert find_clusters(evidences) == []

    def test_expired_evidence_filtered(self):
        now = T0 + timedelta(hours=10)
        evidences = [
            ev(Family.RSI, +1, Timeframe.M5, (990, 1010), valid_until=T0),  # 期限切れ
            ev(Family.GRANVILLE, +1, Timeframe.H1, (995, 1015)),
        ]
        assert find_clusters(evidences, now=now) == []

    def test_higher_tf_weighs_more(self):
        """D1根拠を含むクラスタが上位にソートされる。"""
        clusters = find_clusters([
            ev(Family.RSI, +1, Timeframe.M5, (990, 1010)),
            ev(Family.GRANVILLE, +1, Timeframe.M5, (995, 1015)),
            ev(Family.FIB, -1, Timeframe.D1, (2000, 2020)),
            ev(Family.SR, -1, Timeframe.D1, (2010, 2030)),
        ])
        assert len(clusters) == 2
        assert clusters[0].direction == -1            # D1ペア (3.0+3.0=6.0) が先頭
        assert clusters[0].score == Decimal("6.0")
        assert clusters[1].score == Decimal("2.0")

    def test_min_families_cannot_be_relaxed(self):
        """min_families=1 への緩和は CLAUDE.md 第5条違反として拒否。"""
        with pytest.raises(ValueError, match="rule 5"):
            find_clusters([], min_families=1)

    def test_invalid_evidence_rejected(self):
        with pytest.raises(ValueError, match="invalid zone"):
            ev(Family.RSI, +1, Timeframe.M5, (1010, 990))
        with pytest.raises(ValueError, match="direction"):
            ev(Family.RSI, 0, Timeframe.M5, (990, 1010))

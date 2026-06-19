"""未来裁量エンジンの単体テスト (設計書 §5)。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from infers.strategies.narrow_focus.future_discretion import (
    FutureCell, build_future_map, make_paths, propose_limit_orders,
    rsi_band, sma_forward_linear, sma_slope_sign, sma_touch_curve, sma_touch_price,
)
from infers.indicators import Q, RsiState
from infers.analysis.support_resistance import SRZone

UTC = timezone.utc
NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


def flat_state(rsi_level: str = "neutral") -> RsiState:
    """RSI=50 の中立 Wilder 状態 (avg_gain = avg_loss = 5 ティック)。"""
    return RsiState(period=14, avg_gain=Decimal(5), avg_loss=Decimal(5), last_close_int=1000)


# ---------------------------------------------------------------------------
# §5.2 SMAタッチ曲線 (閉形式解)
# ---------------------------------------------------------------------------

class TestSmaTouchPrice:
    def test_flat_history_touch_equals_price(self):
        """全終値=1000 ならSMAは価格に一致しており、P*(k)=1000 (恒等検算)。"""
        closes = [1000] * 120
        for k in (1, 5, 30, 89):
            assert sma_touch_price(closes, 90, k) == Decimal(1000)

    def test_fixed_point_property(self):
        """P*(k) は接触条件の不動点: 線形パスで P* に向かうと SMA(t0+k) ≈ P*。"""
        closes = list(range(900, 1020))   # 上昇トレンドの履歴 (120本)
        for k in (1, 7, 20, 60):
            p_star = sma_touch_price(closes, 90, k)
            # 不動点検証は整数丸めを避けるため Decimal のまま代入して確認
            c0 = closes[-1]
            s_known = sum(closes[-(90 - k):])
            sum_future = Decimal(k) * c0 + (p_star - c0) * (k + 1) / 2
            sma_at_k = ((s_known + sum_future) / 90).quantize(Q)
            assert abs(sma_at_k - p_star) <= Decimal("0.000000001")

    def test_matches_brute_force_simulation(self):
        """閉形式解 = 愚直シミュレーション (Decimalパスでの直接計算)。"""
        closes = [1000 + (i * 7) % 13 - 6 for i in range(100)]   # 不規則な履歴
        k, m = 5, 90
        p_star = sma_touch_price(closes, m, k)
        c0 = closes[-1]
        path = [Decimal(c0) + (p_star - c0) * i / k for i in range(1, k + 1)]
        window = [Decimal(x) for x in closes[-(m - k):]] + path
        assert len(window) == m
        sma = (sum(window) / m).quantize(Q)
        assert abs(sma - p_star) <= Decimal("0.000000001")

    def test_k1_formula(self):
        """k=1: P* = S_{m-1}/(m-1)。"""
        closes = [1000] * 89 + [1090]     # 直近だけ跳ねた履歴 (90本)
        # S_known(1) = 直近89本 = 1000*88 + 1090
        expected = (Decimal(1000 * 88 + 1090) / 89).quantize(Q)
        assert sma_touch_price(closes, 90, 1) == expected

    def test_bounds_enforced(self):
        closes = [1000] * 120
        with pytest.raises(ValueError, match="1 <= k < period"):
            sma_touch_price(closes, 90, 90)
        with pytest.raises(ValueError, match="1 <= k < period"):
            sma_touch_price(closes, 90, 0)
        with pytest.raises(ValueError, match="known closes"):
            sma_touch_price([1000] * 50, 90, 5)

    def test_touch_curve_sweep(self):
        closes = [1000] * 120
        curve = sma_touch_curve(closes, 90, horizon=10)
        assert list(curve.keys()) == list(range(1, 11))
        assert all(v == Decimal(1000) for v in curve.values())


# ---------------------------------------------------------------------------
# §5.3 RSI逆算バンド (4パス族)
# ---------------------------------------------------------------------------

class TestRsiBand:
    def test_paths_end_exactly_at_target(self):
        paths = make_paths(c0=1000, target_int=900, k=6)
        assert set(paths) == {"linear", "front_loaded", "back_loaded", "retrace"}
        for name, path in paths.items():
            assert len(path) == 6, name
            assert path[-1] == 900, name
            assert all(isinstance(x, int) for x in path), name

    def test_retrace_path_contains_bounce(self):
        """戻り挟みパスは下落途中に上方向の動き(gain)を含む。"""
        path = make_paths(c0=1000, target_int=900, k=9)["retrace"]
        diffs = [b - a for a, b in zip([1000] + path, path)]
        assert any(d > 0 for d in diffs)

    def test_large_drop_certain_oversold(self):
        """大幅下落 (1000→850) なら全パスで RSI<=30 (確実圏)。"""
        band = rsi_band(flat_state(), target_int=850, k=6)
        assert band.oversold_certain
        assert band.oversold_possible
        assert band.lo <= band.hi

    def test_small_drop_not_oversold(self):
        """小幅下落 (1000→990) では売られすぎに届かない。"""
        band = rsi_band(flat_state(), target_int=990, k=6)
        assert not band.oversold_possible
        assert band.lo > Decimal(30)

    def test_large_rise_certain_overbought(self):
        band = rsi_band(flat_state(), target_int=1150, k=6)
        assert band.overbought_certain

    def test_oversold_possible_without_likely(self):
        """possible(いずれかのパスが到達)だが likely(線形パス到達)ではない例。

        target=960,k=12: back_loadedはRSI<=30に到達 (possible) だが
        linearは33.8で30超 (not likely)。M5加点(_likely)は厳格側で不成立。
        """
        band = rsi_band(flat_state(), target_int=960, k=12)
        assert band.oversold_possible
        assert not band.oversold_likely
        assert band.by_path["linear"] > Decimal(30)

    def test_overbought_possible_without_likely(self):
        """target=1040,k=12: back_loadedはRSI>=70に到達 (possible) だが
        linearは66.2で70未満 (not likely)。"""
        band = rsi_band(flat_state(), target_int=1040, k=12)
        assert band.overbought_possible
        assert not band.overbought_likely
        assert band.by_path["linear"] < Decimal(70)

    def test_oversold_likely_when_linear_reaches(self):
        """大幅下落(線形パスも含め全パスが到達)なら likely も成立する。"""
        band = rsi_band(flat_state(), target_int=850, k=6)
        assert band.oversold_likely
        assert band.by_path["linear"] <= Decimal(30)

    def test_band_consistency_and_path_ordering(self):
        """lo/hi は by_path の min/max。反発を挟むと到達時RSIは緩む(高い)。"""
        band = rsi_band(flat_state(), target_int=920, k=9)
        assert band.lo == min(band.by_path.values())
        assert band.hi == max(band.by_path.values())
        assert band.by_path["retrace"] >= band.by_path["linear"]

    def test_state_not_mutated(self):
        """rsi_forward 経由のため元の Wilder 状態は不変 (純粋関数)。"""
        st = flat_state()
        rsi_band(st, target_int=900, k=5)
        assert st.avg_gain == Decimal(5) and st.last_close_int == 1000


# ---------------------------------------------------------------------------
# §5.4 未来コンフルエンスマップ + §5.5 指値候補
# ---------------------------------------------------------------------------

def build_buy_scenario_cells() -> list[FutureCell]:
    """1000から900への下落でRSI極値+SRゾーンが合流するシナリオ。"""
    closes = [1000] * 120
    return build_future_map(
        closes=closes,
        rsi_state=flat_state(),
        direction=+1,
        k_range=range(4, 7),                  # k = 4, 5, 6
        prices=[880, 890, 900, 910, 1000],
        sr_zones=[SRZone(low_int=895, high_int=905, touches=2,
                         strength=Decimal("1.5"), role="SUPPORT")],
        fib_levels=[900],
        fib_tol_ticks=2,
    )


class TestFutureMap:
    def test_confluence_cells_detected(self):
        cells = build_buy_scenario_cells()
        assert cells, "合流セルが検出されるはず"
        at_900 = [c for c in cells if c.price_int == 900]
        assert at_900
        for c in at_900:
            assert "RSI" in c.families and "SR" in c.families and "FIB" in c.families
            # 1 family = 1 点 (手法G2 ①): RSI(1) + SR(1) + FIB(1) = 3。
            # M5前方バンドが売られすぎ可、SRは下方支え、FIBも合流。
            assert c.score == Decimal(3)
            assert c.rsi_strength == "LOW"          # M5のみ (htf指定なし)

    def test_single_family_cells_excluded(self):
        """SR/FIBから外れた価格 (880) はRSIのみ=1family → マップに残らない。"""
        cells = build_buy_scenario_cells()
        assert all(c.price_int != 880 for c in cells)

    def test_no_drop_no_oversold_cell(self):
        """現在値近傍 (1000) はRSI極値に達せず、SRもないため不成立。"""
        cells = build_buy_scenario_cells()
        assert all(c.price_int != 1000 for c in cells)

    def test_score_fib_false_drops_fib_family(self):
        """score_fib=False で FIB はスコア/familyから外れ、SR+RSI のみで判定される。

        900のセルは RSI(1)+SR(1) = 2 で 2 family を満たし残るが、families に
        FIB は含まれず、score も FIB の +1 分だけ下がる (3→2)。
        """
        on = [c for c in build_buy_scenario_cells() if c.price_int == 900][0]
        off_cells = build_future_map(
            closes=[1000] * 120, rsi_state=flat_state(), direction=+1,
            k_range=range(4, 7), prices=[880, 890, 900, 910, 1000],
            sr_zones=[SRZone(low_int=895, high_int=905, touches=2,
                             strength=Decimal("1.5"), role="SUPPORT")],
            fib_levels=[900], fib_tol_ticks=2, score_fib=False)
        off = [c for c in off_cells if c.price_int == 900][0]
        assert "FIB" in on.families and "FIB" not in off.families
        assert off.score == on.score - 1            # FIBの+1が消える

    def test_score_fib_false_filters_fib_only_padding(self):
        """FIBだけで2family目を満たしていた弱いセルは score_fib=False で消える。

        SR/RSI が無く SMA+FIB のみのセルは、FIB除外で SMA 1family → 足切り。
        """
        # 900に SR/RSI 無し・FIBのみ。SMA は無いので FIB単独 = 1 family。
        cells_off = build_future_map(
            closes=[1000] * 120, rsi_state=flat_state(), direction=+1,
            k_range=range(4, 7), prices=[900], fib_levels=[900], fib_tol_ticks=2,
            score_fib=False)
        assert cells_off == []                       # FIB除外で 2family を満たせず消滅

    def test_min_families_cannot_be_relaxed(self):
        with pytest.raises(ValueError, match="rule 5"):
            build_future_map(
                closes=[1000] * 120, rsi_state=flat_state(), direction=+1,
                k_range=range(1, 2), prices=[900], min_families=1,
            )

    def test_sma_touch_counts_as_one_family(self):
        """90/200SMA両方に接触しても 'SMA' 1 family のみ (+RSIで成立)。"""
        closes = [900] * 250                   # SMA90 = SMA200 = 900
        cells = build_future_map(
            closes=[1000] * 0 + closes[:-1] + [1000],  # 末尾c0=1000, SMAは~900近傍
            rsi_state=flat_state(),
            direction=+1,
            k_range=range(5, 6),
            prices=[900],
            sma_periods=(90, 200),
            sma_tol_ticks=10,
        )
        assert len(cells) == 1
        assert cells[0].families.count("SMA") == 1
        assert set(cells[0].families) == {"RSI", "SMA"}

    def test_htf_rsi_aligned_adds_rsi_family(self):
        """上位足RSI極値 (htf_rsi_aligned>=1) で RSI family が 1点 成立する。

        M5は neutral で極値に達しないが、上位足(H1/D1)RSIが売られすぎなら RSI を
        1点加点 (強度 MEDIUM)。SR(1)+RSI(1) = 2 family を満たす (手法G2-⑤③)。
        """
        # price 950 は M5 前方バンド lo≈32 (>30) で oversold に達しない中立域。
        # よって M5 単独では RSI family は立たず、上位足の寄与のみを切り出せる。
        base = dict(closes=[1000] * 120, rsi_state=flat_state(), direction=+1,
                    k_range=range(4, 5), prices=[950],
                    sr_zones=[SRZone(low_int=945, high_int=955, touches=2,
                                     strength=Decimal("1.5"), role="SUPPORT")])
        # 上位足RSI極値なし → SR 1 family のみ → 足切り
        assert build_future_map(**base, htf_rsi_aligned=0) == []
        # 上位足RSI極値1つ → RSI family追加で成立 (1点・強度MEDIUM)
        cells = build_future_map(**base, htf_rsi_aligned=1)
        assert len(cells) == 1
        assert set(cells[0].families) == {"SR", "RSI"}
        assert cells[0].score == Decimal(2)            # SR(1)+RSI(1) — 二値加点
        assert cells[0].rsi_strength == "MEDIUM"       # 上位足のみ

    def test_rsi_score_is_binary_regardless_of_tf_count(self):
        """複数の上位足が極値でも RSI は 1点のまま (手法G2-⑤①。強度に反映)。"""
        base = dict(closes=[1000] * 120, rsi_state=flat_state(), direction=+1,
                    k_range=range(4, 5), prices=[950],
                    sr_zones=[SRZone(low_int=945, high_int=955, touches=2,
                                     strength=Decimal("1.5"), role="SUPPORT")])
        one = build_future_map(**base, htf_rsi_aligned=1)[0]
        two = build_future_map(**base, htf_rsi_aligned=2)[0]
        assert two.score == one.score                  # 重なっても点数は同じ
        assert two.rsi_strength == one.rsi_strength == "MEDIUM"

    def test_rsi_strength_high_medium_low(self):
        """強度マトリクス (手法G2-⑤③): 上位足+M5=HIGH / 上位足のみ=MEDIUM / M5のみ=LOW。"""
        sr900 = [SRZone(low_int=895, high_int=905, touches=2,
                        strength=Decimal("1.5"), role="SUPPORT")]
        sr950 = [SRZone(low_int=945, high_int=955, touches=2,
                        strength=Decimal("1.5"), role="SUPPORT")]
        common = dict(closes=[1000] * 120, rsi_state=flat_state(),
                      direction=+1, k_range=range(4, 5))
        # M5のみ: 900はM5前方バンド売られすぎ、上位足なし
        low = build_future_map(**common, prices=[900], sr_zones=sr900,
                               htf_rsi_aligned=0)[0]
        assert low.rsi_strength == "LOW"
        # 上位足のみ: 950はM5中立、上位足1つ
        med = build_future_map(**common, prices=[950], sr_zones=sr950,
                               htf_rsi_aligned=1)[0]
        assert med.rsi_strength == "MEDIUM"
        # 上位足+M5: 900かつ上位足1つ
        high = build_future_map(**common, prices=[900], sr_zones=sr900,
                                htf_rsi_aligned=1)[0]
        assert high.rsi_strength == "HIGH"

    def test_m5_rsi_family_requires_linear_path(self):
        """M5 RSI familyの加点は線形パス基準(_likely)に厳格化 (G2-⑤訂正2026-06-15)。

        target=960,k=12 は oversold_possible だが oversold_likely ではない
        (linearはRSI>30)。htf寄与なしだと RSI family は成立せず、SRのみ=1family
        で min_families(2)未達 → 足切りされる。
        """
        base = dict(closes=[1000] * 120, rsi_state=flat_state(), direction=+1,
                    k_range=range(12, 13), prices=[960],
                    sr_zones=[SRZone(low_int=955, high_int=965, touches=2,
                                     strength=Decimal("1.5"), role="SUPPORT")])
        assert build_future_map(**base, htf_rsi_aligned=0) == []

    def test_rsi_conflict_destroys_cluster(self):
        """相反 (上位足が逆方向の極値) は他根拠があってもクラスタ破壊 (手法G2-⑤③)。"""
        base = dict(closes=[1000] * 120, rsi_state=flat_state(), direction=+1,
                    k_range=range(4, 5), prices=[900],
                    sr_zones=[SRZone(low_int=895, high_int=905, touches=2,
                                     strength=Decimal("1.5"), role="SUPPORT")])
        # 相反なし: M5売られすぎ(RSI) + SR で 2 family 成立
        assert len(build_future_map(**base, htf_rsi_conflict=0)) == 1
        # 相反あり: 上位足が買われすぎ → NO-TRADE (全セル破棄)
        assert build_future_map(**base, htf_rsi_conflict=1) == []

    def test_sr_resistance_touch_destroys_cluster(self):
        """到達価格の上方に抵抗帯 (重心 > p) があるとクラスタ破壊 (手法G2-⑥③)。"""
        common = dict(closes=[1000] * 120, rsi_state=flat_state(),
                      direction=+1, k_range=range(4, 5), prices=[900])
        # サポート (重心 ≤ 900): RSI(M5)+SR で成立
        support = build_future_map(
            **common, sr_zones=[SRZone(low_int=895, high_int=905, touches=2,
                                       strength=Decimal("1.5"), role="SUPPORT")])
        assert len(support) == 1 and set(support[0].families) == {"RSI", "SR"}
        # レジスタンス (重心=925 > 900): 抵抗帯接触 → クラスタ破壊
        resistance = build_future_map(
            **common, sr_zones=[SRZone(low_int=900, high_int=950, touches=2,
                                       strength=Decimal("1.5"), role="RESISTANCE")])
        assert resistance == []

    def test_sr_strength_recorded(self):
        """接触サポートゾーンの減衰加重強度が FutureCell に記録される (手法G2-⑥④)。"""
        cell = build_future_map(
            closes=[1000] * 120, rsi_state=flat_state(), direction=+1,
            k_range=range(4, 5), prices=[900],
            sr_zones=[SRZone(low_int=895, high_int=905, touches=3,
                             strength=Decimal("2.7"), role="SUPPORT")])[0]
        assert cell.sr_strength == Decimal("2.7")


class TestSmaGranville:
    """SMA/グランビル詳細仕様 (手法G2-⑦): 傾き整合タッチ(買②③)・極端乖離(買④)・強度。"""

    def test_slope_sign_helper(self):
        assert sma_slope_sign(list(range(900, 1020)), 90, 5) == +1   # 上昇
        assert sma_slope_sign([1000] * 120, 90, 5) == 0              # 横這い
        assert sma_slope_sign(list(range(1119, 999, -1)), 90, 5) == -1  # 下降
        assert sma_slope_sign([1000] * 92, 90, 5) == 0              # 履歴不足→0
        assert sma_slope_sign([1000] * 95, 90, 5) == 0              # ちょうど境界

    def test_touch_aligned_slope_is_high(self):
        """買②③: SMAタッチ + 傾き順行(横這い含む)→ SMA family・強度HIGH。"""
        # 横這い1000、p=1000でSMAに接触。RSIは中立なのでSRで2family目を作る。
        cells = build_future_map(
            closes=[1000] * 120, rsi_state=flat_state(), direction=+1,
            k_range=range(4, 5), prices=[1000], sma_tol_ticks=2,
            sr_zones=[SRZone(low_int=995, high_int=1005, touches=2,
                             strength=Decimal("1.5"), role="SUPPORT")])
        assert len(cells) == 1
        assert "SMA" in cells[0].families and cells[0].sma_strength == "HIGH"

    def test_touch_against_slope_dropped(self):
        """傾き逆行のタッチは根拠にしない: 同一の下降終値/接触価格でも買いはSMA不成立、
        売り(傾き順行)はSMA成立する (手法G2-⑦③ 「傾きが逆行していない」)。"""
        falling = list(range(1119, 999, -1))         # 下降, slope<0, c0=1000
        # k=4,p=1040 で proj≈1041.7 → タッチ圏 (tol=2)。p>c0 なので RSI は中立。
        common = dict(closes=falling, rsi_state=flat_state(),
                      k_range=range(4, 5), prices=[1040], sma_tol_ticks=2)
        # 売り(戻り): 傾き(下降)は売りに順行 → SMA成立 (レジスタンスで2family目)
        sell = build_future_map(
            **common, direction=-1,
            sr_zones=[SRZone(low_int=1035, high_int=1045, touches=2,
                             strength=Decimal("1.5"), role="RESISTANCE")])
        assert len(sell) == 1 and "SMA" in sell[0].families
        # 買い: 下降SMAへの下からのタッチは逆行 → SMA不成立 → SR単独=1family→消滅
        buy = build_future_map(
            **common, direction=+1,
            sr_zones=[SRZone(low_int=1035, high_int=1045, touches=2,
                             strength=Decimal("1.5"), role="SUPPORT")])
        assert buy == []

    def test_buy4_extreme_deviation_medium(self):
        """買④: 価格がSMAから極端に下方乖離 → SMA family・強度MEDIUM (要他根拠)。"""
        base = dict(closes=[1000] * 120, rsi_state=flat_state(), direction=+1,
                    k_range=range(4, 5), prices=[900])      # 900はSMA~993から大きく乖離
        # far閾値なし(既定OFF) → 乖離は根拠にならず RSI単独=1family → 消滅
        assert build_future_map(**base) == []
        # far=50 を与えると 買④ 成立 (RSI(M5)+SMA で2family)
        cells = build_future_map(**base, sma_far_ticks=50)
        assert len(cells) == 1
        assert set(cells[0].families) == {"RSI", "SMA"}
        assert cells[0].sma_strength == "MEDIUM"

    def test_density_two_periods_is_high(self):
        """複数SMA(90/200)が同時にヒットすると密集として強度HIGH (買④単独はMEDIUM)。"""
        base = dict(closes=[1000] * 250, rsi_state=flat_state(), direction=+1,
                    k_range=range(4, 5), prices=[900], sma_far_ticks=50)
        one = build_future_map(**base, sma_periods=(90,))[0]
        two = build_future_map(**base, sma_periods=(90, 200))[0]
        assert one.sma_strength == "MEDIUM"             # 単一期間の買④
        assert two.sma_strength == "HIGH"               # 90/200密集
        assert one.score == two.score                   # 点数は重なっても同じ (二値)

    def test_sma_score_is_binary(self):
        """SMA family は touch でも deviation でも 1点 (手法G2-⑦①)。"""
        touch = build_future_map(
            closes=[1000] * 120, rsi_state=flat_state(), direction=+1,
            k_range=range(4, 5), prices=[1000], sma_tol_ticks=2,
            sr_zones=[SRZone(low_int=995, high_int=1005, touches=2,
                             strength=Decimal("1.5"), role="SUPPORT")])[0]
        # families = {SMA, SR} → score 2 (各1点)
        assert touch.score == Decimal(2) and set(touch.families) == {"SMA", "SR"}


class TestDowFamily:
    """ダウ順行 (手法G2-⑧): 順行/反転初動→1点、明確な逆行→クラスタ破壊。"""

    def test_dow_aligned_adds_family(self):
        """dow_aligned で "DOW" family が 1点 成立し、強度が記録される。"""
        base = dict(closes=[1000] * 120, rsi_state=flat_state(), direction=+1,
                    k_range=range(4, 5), prices=[900],
                    sr_zones=[SRZone(low_int=895, high_int=905, touches=2,
                                     strength=Decimal("1.5"), role="SUPPORT")])
        # dow なし: RSI(M5)+SR = 2 family
        without = build_future_map(**base)[0]
        assert "DOW" not in without.families
        # dow 順行: +1 されて DOW family が加わり強度 HIGH
        with_dow = build_future_map(**base, dow_aligned=True, dow_strength="HIGH")[0]
        assert "DOW" in with_dow.families
        assert with_dow.dow_strength == "HIGH"
        assert with_dow.score == without.score + 1

    def test_dow_conflict_destroys_all(self):
        """dow_conflict (明確な逆行) は他根拠が揃っていても全セル破棄 (NO-TRADE)。"""
        base = dict(closes=[1000] * 120, rsi_state=flat_state(), direction=+1,
                    k_range=range(4, 7), prices=[880, 890, 900, 910],
                    sr_zones=[SRZone(low_int=895, high_int=905, touches=2,
                                     strength=Decimal("1.5"), role="SUPPORT")],
                    fib_levels=[900], fib_tol_ticks=2)
        assert build_future_map(**base) != []                    # 相反なしなら成立
        assert build_future_map(**base, dow_conflict=True) == []  # 相反 → 全破棄

    def test_dow_family_enables_two_family_with_rsi(self):
        """RSI単独(1 family)では足切りだが、DOW順行が加わると 2 family で成立。"""
        base = dict(closes=[1000] * 120, rsi_state=flat_state(), direction=+1,
                    k_range=range(4, 5), prices=[900])           # 900: M5売られすぎのみ
        assert build_future_map(**base) == []                    # RSI単独 → 足切り
        cells = build_future_map(**base, dow_aligned=True, dow_strength="MEDIUM")
        assert len(cells) == 1
        assert set(cells[0].families) == {"DOW", "RSI"}
        assert cells[0].dow_strength == "MEDIUM"


class TestProposeLimitOrders:
    BAR = timedelta(minutes=5)

    def test_candidate_with_expiry(self):
        """失効時刻 = now + k_max × バー長 が必ず付与される (設計書 §5.5)。"""
        cells = build_buy_scenario_cells()
        orders = propose_limit_orders(
            cells, direction=+1, now=NOW, bar_duration=self.BAR,
            price_step=10, invalidation_price=870,
        )
        assert len(orders) == 1                # 成立セルは900のみ → 1候補
        o = orders[0]
        assert o.limit_price_int == 900        # 最高スコアセルの価格
        k_min, k_max = o.eta_window
        assert (k_min, k_max) == (4, 6)
        assert o.expiry == NOW + 6 * self.BAR  # ★ 時間依存の失効
        assert o.invalidation_price == 870
        assert o.direction == +1
        assert o.rsi_band[0] <= o.rsi_band[1]

    def test_non_adjacent_prices_split_candidates(self):
        def cell(p: int, k: int, score: str) -> FutureCell:
            band = rsi_band(flat_state(), p, k)
            return FutureCell(k=k, price_int=p, families=("RSI", "SR"),
                              score=Decimal(score), rsi=band)

        orders = propose_limit_orders(
            [cell(900, 5, "2"), cell(950, 5, "3")],   # step=10 で非隣接
            direction=+1, now=NOW, bar_duration=self.BAR, price_step=10,
        )
        assert len(orders) == 2
        assert orders[0].score == Decimal(3)          # スコア降順
        assert orders[0].limit_price_int == 950

    def test_empty_cells(self):
        assert propose_limit_orders([], direction=+1, now=NOW,
                                    bar_duration=self.BAR, price_step=10) == []

    def test_rsi_band_synthesized_across_k_at_price(self):
        """rsi_band は採用価格の全 k × 全パス族で合成され、k=1 の縮退点に
        固定されない (P4)。

        同一価格 900 に k=1 (4パス縮退 → lo==hi) と k=6 (経路発散 → lo<hi) が
        並ぶ。best のタイブレークは最小 k (=1, 縮退) を選ぶが、band は両 k の
        min lo / max hi で合成されるため幅を持つ。
        """
        c1 = FutureCell(k=1, price_int=900, families=("RSI", "SR"),
                        score=Decimal("2"), rsi=rsi_band(flat_state(), 900, 1))
        c6 = FutureCell(k=6, price_int=900, families=("RSI", "SR"),
                        score=Decimal("2"), rsi=rsi_band(flat_state(), 900, 6))
        assert c1.rsi.lo == c1.rsi.hi          # k=1 は縮退点 (旧バグの温床)
        assert c6.rsi.lo < c6.rsi.hi           # k=6 は経路依存で幅あり

        orders = propose_limit_orders(
            [c1, c6], direction=+1, now=NOW, bar_duration=self.BAR, price_step=10)
        assert len(orders) == 1
        o = orders[0]
        assert o.limit_price_int == 900
        assert o.eta_window == (1, 6)          # 採用価格の k 範囲
        # 合成バンド = 両セルの min lo / max hi (縮退点 c1 のみではない)
        assert o.rsi_band[0] == min(c1.rsi.lo, c6.rsi.lo)
        assert o.rsi_band[1] == max(c1.rsi.hi, c6.rsi.hi)
        assert o.rsi_band[0] < o.rsi_band[1]   # もはや点ではない

    def test_eta_window_is_per_price_not_whole_group(self):
        """eta_window は採用 limit 価格に固有の k 範囲であり、群全体の k 域では
        ない (P5: 群全体だと価格レンジ分 k がほぼ全域化し情報価値を失う)。
        """
        def cell(p: int, k: int, score: str) -> FutureCell:
            return FutureCell(k=k, price_int=p, families=("RSI", "SR"),
                              score=Decimal(score), rsi=rsi_band(flat_state(), p, k))

        # 900 (k=2, 高スコア=採用) と隣接する 910 (k=6) が1群を成す
        orders = propose_limit_orders(
            [cell(900, 2, "3"), cell(910, 6, "2")],
            direction=+1, now=NOW, bar_duration=self.BAR, price_step=10)
        assert len(orders) == 1                # 隣接 → 1群
        o = orders[0]
        assert o.limit_price_int == 900        # 最高スコア価格
        assert o.eta_window == (2, 2)          # 900 固有の k (群全体の (2,6) ではない)
        assert o.expiry == NOW + 2 * self.BAR  # k_max も採用価格基準

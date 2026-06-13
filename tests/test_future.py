"""未来裁量エンジンの単体テスト (設計書 §5)。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from infers.analysis.future_discretion import (
    FutureCell, build_future_map, make_paths, propose_limit_orders,
    rsi_band, sma_forward_linear, sma_touch_curve, sma_touch_price,
)
from infers.analysis.indicators import Q, RsiState
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
            # RSIは戻り挟みパスで30をわずかに超えるため「パス次第」= 0.5寄与。
            # SR(1) + FIB(1) + RSI(0.5) = 2.5 以上
            assert c.score >= Decimal("2.5")

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

        900のセルは SR(1)+RSI(0.5) = 1.5 で 2 family を満たし残るが、families に
        FIB は含まれず、score も FIB の +1 分だけ下がる。
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

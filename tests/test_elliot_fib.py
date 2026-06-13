"""エリオット波動カウンター・フィボナッチ投影の単体テスト。

3原則違反の即無効化と、無効化価格 (invalidation_price) が
シナリオ破棄ラインとして O(1) で機能することを検証する。
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from infers.analysis.elliot import ElliottCounter, WaveCount, count_waves
from infers.analysis.fibonacci import project, project_wave3, project_wave5
from infers.analysis.zigzag import SwingPoint
from infers.data.models import Timeframe

UTC = timezone.utc
T0 = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


def sw(kind: str, price: int, i: int) -> SwingPoint:
    t = T0 + i * Timeframe.H1.duration
    return SwingPoint(kind=kind, bar_time=t, price_int=price, tf=Timeframe.H1,
                      confirmed_at=t + Timeframe.H1.duration)


def bull_impulse(*prices: int) -> tuple[SwingPoint, ...]:
    """LOW起点で交互に並ぶスイング列を生成 (上昇推進波の素材)。"""
    kinds = ["LOW", "HIGH"] * 3
    return tuple(sw(kinds[i], p, i) for i, p in enumerate(prices))


# 正常な上昇推進波: len1=50, w2押し60%, len3=90, w4押し50%, len5=75
VALID = (100, 150, 120, 210, 165, 240)


def best_anchor_p0(view, p0_price: int) -> WaveCount | None:
    """P0価格でアンカーされた候補を取り出す。"""
    for c in view.candidates:
        if c.pivots[0].price_int == p0_price:
            return c
    return None


class TestThreeRules:
    def test_valid_full_impulse_accepted(self):
        view = count_waves(bull_impulse(*VALID))
        wc = best_anchor_p0(view, 100)
        assert wc is not None
        assert wc.complete and wc.current_wave == 5
        assert wc.direction == 1

    def test_rule2_violation_invalidates(self):
        """原則②: 第2波が第1波始点(100)を割る → カウント即無効。"""
        view = count_waves(bull_impulse(100, 150, 95))
        assert best_anchor_p0(view, 100) is None

    def test_rule3_violation_invalidates(self):
        """原則③: 第4波が第1波高値(150)を割る → カウント即無効。"""
        view = count_waves(bull_impulse(100, 150, 120, 210, 145))
        assert best_anchor_p0(view, 100) is None

    def test_rule1_violation_invalidates(self):
        """原則①: len1=100, len3=80, len5=90 → 第3波最短でカウント無効。"""
        view = count_waves(bull_impulse(100, 200, 150, 230, 210, 300))
        assert best_anchor_p0(view, 100) is None

    def test_rule1_ok_when_wave5_shortest(self):
        """len5=70 < len3=80 < len1=100 は第5波最短なので有効。"""
        view = count_waves(bull_impulse(100, 200, 150, 230, 210, 280))
        assert best_anchor_p0(view, 100) is not None


class TestInvalidationPrice:
    def test_rule2_phase_invalidation_is_p0(self):
        """波1〜3進行中の無効化価格 = P0 (原則②)。"""
        for prices in [(100, 150), (100, 150, 120), (100, 150, 120, 210)]:
            wc = best_anchor_p0(count_waves(bull_impulse(*prices)), 100)
            assert wc is not None
            assert wc.invalidation_price == 100
            assert wc.is_invalidated(99)        # 割れたら即無効 (O(1)比較)
            assert not wc.is_invalidated(100)   # 同値はまだ有効
            assert not wc.is_invalidated(101)

    def test_rule3_phase_invalidation_is_p1(self):
        """波4〜5進行中の無効化価格 = P1 (原則③)。"""
        wc = best_anchor_p0(count_waves(bull_impulse(100, 150, 120, 210, 165)), 100)
        assert wc is not None
        assert wc.invalidation_price == 150
        assert wc.is_invalidated(149)
        assert not wc.is_invalidated(150)

    def test_rule1_cap_during_wave5(self):
        """第5波進行中 len3(80) < len1(100) → キャップ = P4 + len3 = 290。"""
        wc = best_anchor_p0(count_waves(bull_impulse(100, 200, 150, 230, 210)), 100)
        assert wc is not None
        assert wc.max_wave5_price == 290
        assert wc.is_invalidated(291)           # 超えると第3波最短が確定
        assert not wc.is_invalidated(290)

    def test_no_cap_when_wave3_longest(self):
        """len3 >= len1 ならキャップは立たない (第5波は無制限に伸びてよい)。"""
        wc = best_anchor_p0(count_waves(bull_impulse(*VALID[:5])), 100)
        assert wc is not None
        assert wc.max_wave5_price is None

    def test_bear_direction_mirrored(self):
        """下降推進波: 無効化は上抜け方向。"""
        kinds = ["HIGH", "LOW", "HIGH"]
        swings = tuple(sw(kinds[i], p, i) for i, p in enumerate((200, 150, 180)))
        view = count_waves(swings)
        wc = best_anchor_p0(view, 200)
        assert wc is not None and wc.direction == -1
        assert wc.invalidation_price == 200
        assert wc.is_invalidated(201)           # 始点の上抜けで無効
        assert not wc.is_invalidated(199)


class TestElliottCounter:
    def test_incremental_and_ambiguity(self):
        ec = ElliottCounter()
        view = None
        kinds = ["LOW", "HIGH"] * 3
        for i, p in enumerate(VALID):
            view = ec.on_swing(sw(kinds[i], p, i))
        assert view is not None and len(view.candidates) >= 1
        # スコア降順
        scores = [c.score for c in view.candidates]
        assert scores == sorted(scores, reverse=True)
        assert view.ambiguity >= 0

    def test_alternation_contract_enforced(self):
        ec = ElliottCounter()
        ec.on_swing(sw("LOW", 100, 0))
        with pytest.raises(ValueError, match="alternate"):
            ec.on_swing(sw("LOW", 90, 1))


class TestFibonacci:
    def test_wave3_targets(self):
        """len1=50 を P2=120 に投影: 161.8%→120+80.9→201, 261.8%→120+130.9→251。"""
        wc = best_anchor_p0(count_waves(bull_impulse(100, 150, 120)), 100)
        t = project_wave3(wc)
        assert t is not None
        assert t.base_len == 50 and t.anchor.price_int == 120 and t.direction == 1
        assert t.levels == {"100.0": 170, "161.8": 201, "261.8": 251}

    def test_wave5_targets(self):
        """len1=50 を P4=165 に投影。"""
        wc = best_anchor_p0(count_waves(bull_impulse(100, 150, 120, 210, 165)), 100)
        t = project_wave5(wc)
        assert t is not None
        assert t.anchor.price_int == 165
        assert t.levels == {"100.0": 215, "161.8": 246, "261.8": 296}

    def test_wave5_requires_p4(self):
        wc = best_anchor_p0(count_waves(bull_impulse(100, 150, 120)), 100)
        assert project_wave5(wc) is None

    def test_bear_targets_subtract(self):
        """下降波では投影はアンカーから下方向。len1=50, P2=180。"""
        kinds = ["HIGH", "LOW", "HIGH"]
        swings = tuple(sw(kinds[i], p, i) for i, p in enumerate((200, 150, 180)))
        wc = best_anchor_p0(count_waves(swings), 200)
        t = project_wave3(wc)
        assert t.levels == {"100.0": 130, "161.8": 99, "261.8": 49}

    def test_project_collects_available(self):
        wc = best_anchor_p0(count_waves(bull_impulse(*VALID[:5])), 100)
        targets = project(wc)
        assert [t.target_wave for t in targets] == [3, 5]

    def test_rounding_half_even_int_ticks(self):
        """端数はROUND_HALF_EVENで整数ティック化 (Decimal規約)。"""
        wc = best_anchor_p0(count_waves(bull_impulse(100, 125, 110)), 100)  # len1=25
        t = project_wave3(wc)
        # 25*1.618=40.45→40, 25*2.618=65.45→65
        assert t.levels == {"100.0": 135, "161.8": 150, "261.8": 175}
        assert all(isinstance(v, int) for v in t.levels.values())

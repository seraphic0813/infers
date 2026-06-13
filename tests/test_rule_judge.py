"""決定論ルールゲート (rule_judge.py) の単体テスト。

narrow_focus_v3.md §5 の NO_GO 4条件 + GO の confidence 加点を検証する。
features の値は narrow_focus_v3.md §6 (Few-shot) の例を流用し、
LLMが下していた decision とルールベースの decision が一致することを
確認する。
"""

from decimal import Decimal

from infers.ai.gateway import JudgementKind, JudgementRequest
from infers.ai.rule_judge import RuleBasedLlmClient, judge_features

BASE_FEATURES = {
    "dow_state": "UP",
    "current_wave": "2",
    "ambiguity": "0.83",
    "cluster_score": "4.0",
    "families": "RSI,SMA,SR,FIB",
    "limit": "251620",
    "invalidation": "251556",
    "w1_high": "251870",
    "rsi": "58.3",
    "rsi_band": "27.40..31.85",
    "eta_bars": "2-6",
    "atr": "100",
}


def features(**overrides) -> dict:
    f = dict(BASE_FEATURES)
    f.update(overrides)
    return f


class TestFewShotParity:
    """narrow_focus_v3.md §6 の3例で decision がLLM版と一致することを確認。"""

    def test_example1_strong_go(self):
        """買い・RSI確実圏近傍 + RR良好 + 一意な波カウント → GO (強)。"""
        v = judge_features(1, BASE_FEATURES)
        assert v.decision == "GO"
        assert Decimal("0.7") <= v.confidence <= Decimal("0.9")
        assert v.invalidation_price == 251556

    def test_example2_standard_go_sma_led(self):
        """買い・SMA主導でRSIはパス次第、eta窓が広くても棄却しない → GO。"""
        f = features(ambiguity="0.42", cluster_score="3.0",
                      families="SMA,SR,FIB", limit="181537",
                      invalidation="181480", w1_high="181760",
                      rsi="49.54", rsi_band="31.20..38.90", eta_bars="1-9")
        v = judge_features(1, f)
        assert v.decision == "GO"

    def test_example3_no_go_tight_risk_and_rr(self):
        """売り・risk極小 + RR劣後 + RSI極値未到達 → NO_GO。"""
        f = features(dow_state="DOWN", ambiguity="0.12", cluster_score="2.0",
                      families="SMA,SR", limit="203372", invalidation="203376",
                      w1_high="203360", rsi="61.2", rsi_band="63.5..69.1",
                      eta_bars="4-12", atr="20")
        v = judge_features(-1, f)
        assert v.decision == "NO_GO"


class TestStructuralFragility:
    """§5-1: risk = |limit - invalidation| が risk_floor 以下 → NO_GO。"""

    def test_tiny_risk_is_no_go(self):
        f = features(limit="251620", invalidation="251618", atr="100")  # risk=2 <= 30
        v = judge_features(1, f)
        assert v.decision == "NO_GO"
        assert "risk" in v.reasons[0]

    def test_risk_above_floor_passes(self):
        f = features(limit="251620", invalidation="251556", atr="100")  # risk=64 > 30
        v = judge_features(1, f)
        assert v.decision == "GO"

    def test_min_ticks_floor_applies_when_atr_zero(self):
        f = features(limit="251620", invalidation="251619", atr="0")  # risk=1 <= floor(1)
        v = judge_features(1, f)
        assert v.decision == "NO_GO"


class TestRiskRewardInferior:
    """§5-2: reward_ref < risk * 0.5 → NO_GO。"""

    def test_reward_ref_too_small_is_no_go(self):
        # risk=64, reward_ref = |251650-251620| = 30 < 64*0.5=32
        f = features(limit="251620", invalidation="251556", w1_high="251650")
        v = judge_features(1, f)
        assert v.decision == "NO_GO"
        assert "RR" in v.reasons[0]

    def test_reward_ref_exactly_half_passes(self):
        # risk=64, reward_ref = |251652-251620| = 32 == 64*0.5 (劣後条件は < のみ)
        f = features(limit="251620", invalidation="251556", w1_high="251652")
        v = judge_features(1, f)
        assert v.decision == "GO"


class TestEmptyConfluence:
    """§5-3: rsi_bandが極値圏外 かつ familiesにSMA無し → NO_GO。"""

    def test_no_rsi_extreme_no_sma_is_no_go(self):
        f = features(families="RSI,SR,FIB", rsi_band="45.0..50.0")
        v = judge_features(1, f)
        assert v.decision == "NO_GO"
        assert "中核根拠" in v.reasons[0]

    def test_no_rsi_extreme_but_sma_present_is_go(self):
        f = features(families="SMA,SR,FIB", rsi_band="45.0..50.0")
        v = judge_features(1, f)
        assert v.decision == "GO"

    def test_sell_side_overbought_certain(self):
        f = features(dow_state="DOWN", families="RSI,SR", limit="203372",
                      invalidation="203440", w1_high="203200",
                      rsi_band="71.0..75.0", atr="20")
        v = judge_features(-1, f)
        assert v.decision == "GO"
        assert v.confidence >= Decimal("0.65")


class TestDataAnomaly:
    """§5-4: dow_state/direction不整合、invalidationが利益方向 → NO_GO。"""

    def test_dow_state_direction_mismatch(self):
        f = features(dow_state="UP")
        v = judge_features(-1, f)
        assert v.decision == "NO_GO"

    def test_invalidation_on_profit_side_for_buy(self):
        f = features(limit="251620", invalidation="251700")  # invalidation > limit (買い)
        v = judge_features(1, f)
        assert v.decision == "NO_GO"

    def test_invalidation_on_profit_side_for_sell(self):
        f = features(dow_state="DOWN", limit="203372", invalidation="203300")
        v = judge_features(-1, f)
        assert v.decision == "NO_GO"


class TestRuleBasedLlmClient:
    def test_judge_delegates_to_judge_features(self):
        req = JudgementRequest(kind=JudgementKind.FUTURE_CONFLUENCE_REVIEW,
                                symbol="XAUUSD", direction=1, features=BASE_FEATURES)
        client = RuleBasedLlmClient()
        v_l1 = client.judge(req, "L1")
        v_l2 = client.judge(req, "L2")
        assert v_l1 == v_l2 == judge_features(1, BASE_FEATURES)

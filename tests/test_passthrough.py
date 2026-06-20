"""手法非依存パススルー・ゲート (`--ai-client none` / spec.md §5.7 案A) の検証。

market_tpsl/smc_bos 等、Narrow Focus 固有特徴量 (dow_state/w1_high/rsi_band/
families) を前提としない手法は、既定のルールゲート (rule_judge.judge_features)
では KeyError → GUARDRAIL NO_GO になり1トレードも約定できない。本ゲートは
features の内容を一切解釈せず常に GO を返すことでこれを解消する。
"""

from __future__ import annotations

import argparse
from decimal import Decimal

from infers.ai.gateway import AiGateway, JudgementKind, JudgementRequest, Tier, VerdictCache
from infers.ai.passthrough import PASSTHROUGH_POLICY, PassthroughLlmClient


def _smc_request(**features: str) -> JudgementRequest:
    """smc_bos 想定の features (Narrow Focus 固有キーを一切持たない)。"""
    return JudgementRequest(kind=JudgementKind.ENTRY_GATE, symbol="XAUUSD",
                            direction=+1, features={"strategy": "smc_bos", **features})


class TestPassthroughPolicy:
    def test_always_l1_only_for_typical_values(self):
        assert PASSTHROUGH_POLICY.decide(Decimal(3), Decimal(0)) is Tier.L1_ONLY
        assert PASSTHROUGH_POLICY.decide(Decimal(0), Decimal(1)) is Tier.L1_ONLY

    def test_always_l1_only_for_edge_values(self):
        """cluster_score/ambiguity が異常値(負・極端)でも NONE/L2 に振れない。"""
        assert PASSTHROUGH_POLICY.decide(Decimal(-100), Decimal(-100)) is Tier.L1_ONLY
        assert PASSTHROUGH_POLICY.decide(Decimal(10**6), Decimal(10**6)) is Tier.L1_ONLY


class TestPassthroughLlmClient:
    def test_always_go_regardless_of_features(self):
        client = PassthroughLlmClient()
        v = client.judge(_smc_request(), "L1")
        assert v.decision == "GO"
        assert v.confidence == Decimal(1)

    def test_does_not_require_narrow_focus_keys(self):
        """dow_state 等が無い features でも例外を出さない (KeyError 不発)。"""
        client = PassthroughLlmClient()
        empty_request = JudgementRequest(kind=JudgementKind.ENTRY_GATE, symbol="XAUUSD",
                                         direction=-1, features={})
        v = client.judge(empty_request, "L2")
        assert v.decision == "GO"


class TestPassthroughGatewayIntegration:
    """AiGateway 経由 (キャッシュ・統計込み) で実際に GO が出ることを確認。"""

    def test_smc_features_get_go_not_guardrail(self):
        gw = AiGateway(client=PassthroughLlmClient(), cache=VerdictCache(),
                       policy=PASSTHROUGH_POLICY)
        v = gw.judge(_smc_request(), cluster_score=Decimal(3), ambiguity=Decimal(0))
        assert v.decision == "GO"
        assert v.source != "GUARDRAIL"

    def test_second_call_hits_cache(self):
        gw = AiGateway(client=PassthroughLlmClient(), cache=VerdictCache(),
                       policy=PASSTHROUGH_POLICY)
        gw.judge(_smc_request(tag="a"), cluster_score=Decimal(3), ambiguity=Decimal(0))
        v2 = gw.judge(_smc_request(tag="a"), cluster_score=Decimal(3), ambiguity=Decimal(0))
        assert v2.source == "CACHE"


class TestCliWiring:
    """main._build_gateway: --ai-client none → パススルー配線。"""

    def test_ai_client_none_returns_go_for_smc_features(self):
        from infers.main import _build_gateway
        args = argparse.Namespace(ai_client="none", verdict_cache=":memory:")
        gw = _build_gateway(args, cache_only=True)
        v = gw.judge(_smc_request(), cluster_score=Decimal(3), ambiguity=Decimal(0))
        assert v.decision == "GO"

    def test_ai_client_none_available_in_choices(self):
        from infers.main import parse_args
        args = parse_args(["--mode", "backtest", "--ai-client", "none"])
        assert args.ai_client == "none"

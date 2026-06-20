"""手法非依存のパススルー・ゲート (`--ai-client none` / spec.md §5.7 案A)。

`rule_judge.RuleBasedLlmClient` は narrow_focus_v3.md §5 の判定基準
(`dow_state`/`w1_high`/`rsi_band`/`families` 等)を前提とするため、別ロジックの
手法(`market_tpsl`・`smc_bos` 等)の features では KeyError → AiGateway の
ガードレールに吸収されて GUARDRAIL NO_GO になり、1トレードも約定できない
(phase2-architecture.md §段階2.5 で判明した既知制約)。

これらの手法はそもそも LLM/ルールゲートを意味的に必要としない
(エントリー判定が provider 内で完結する決定論ゲートそのもの)。本モジュールは
常に GO を返す最小クライアントと、常に L1_ONLY に解決するポリシーを提供し、
ゲートを「存在するが何も判定しない」形で手法非依存に無効化する。

安全原則への影響なし: 防御層(SL単調性・冪等ID・リスク拒否権)は
ExecutionModel/TradingLoop 側で別途・全手法に等しく強制され続ける
(CLAUDE.md §0)。本モジュールが緩めるのは「新規エントリーの約定可否」のみ。
"""

from __future__ import annotations

from decimal import Decimal

from infers.ai.gateway import EscalationPolicy, JudgementRequest, Verdict

# score_l1 を負値にして Tier.NONE(score_l1未満で即NO_GO)を起こりえなくし、
# score_l2/ambiguity_gray を極端値にして Tier.L2_AFTER_L1 も起こりえなくする。
# 結果、cluster_score/ambiguity の値に関わらず常に Tier.L1_ONLY に解決する
# (L2 課金・L2予算管理を一切発生させない最小経路)。
PASSTHROUGH_POLICY = EscalationPolicy(
    score_l1=Decimal(-10**9),
    score_l2=Decimal(10**9),
    ambiguity_gray=Decimal(-10**9),
    l2_daily_call_cap=0,
)


class PassthroughLlmClient:
    """`LlmClient` プロトコルのドロップイン実装。常に GO を返す。

    rule_judge.RuleBasedLlmClient と同様、純粋関数的で例外・タイムアウト・
    予算管理は不要 (常時・$0)。features の内容を一切解釈しないため、
    どの手法の特徴量スキーマであっても KeyError を起こさない。
    """

    def judge(self, request: JudgementRequest, tier: str) -> Verdict:  # noqa: ARG002
        return Verdict(decision="GO", confidence=Decimal(1),
                       reasons=["--ai-client none: ゲートをパススルー(常時GO)"],
                       source="PASSTHROUGH")

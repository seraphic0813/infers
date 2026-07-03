"""AI判断層: Gateway・VerdictCache・決定論ゲート (設計書 §7)。

トレード判定にLLMは現在不使用(正式方針。CLAUDE.md A-12)。LLM実呼び出し系
(`AnthropicLlmClient`・`ai/batch.py`)は撤去済み(改善プラン T6)。
"""

from infers.ai.gateway import (
    MODELS, PROMPT_VERSION, AiGateway, EscalationPolicy,
    JudgementKind, JudgementRequest, LlmClient, Tier, Verdict, VerdictCache, cache_key,
)
from infers.ai.rule_judge import RuleBasedLlmClient, judge_features

__all__ = [
    "AiGateway", "EscalationPolicy", "JudgementKind",
    "JudgementRequest", "LlmClient", "Tier", "Verdict", "VerdictCache",
    "cache_key", "MODELS", "PROMPT_VERSION",
    "RuleBasedLlmClient", "judge_features",
]

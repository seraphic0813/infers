"""AI Gateway — 3層ハイブリッド判断 (設計書 §7 / CLAUDE.md 第13〜16条)。

  L0: Python決定論層 (本モジュールの呼び出し側。無料・常時)
  L1: claude-haiku-4-5  — 一次トリアージ
  L2: claude-fable-5    — 最終ジャッジ (高曖昧性・勝負所のみ)

安全原則 (CLAUDE.md 第1条):
  - 本ゲートウェイは「新規エントリーのゲート」専用。防御 (SL/利確) は
    execution/ が LLM 非依存で完結しており、ここが全停止しても防御は動き続ける
  - LLM例外・タイムアウト・予算超過・スキーマ不整合 → すべて NO_GO
    (DEFAULT NO-TRADE)。例外は決して上位へ伝播しない
  - エスカレーション判断は決定論的なポリシー関数 (ジャーナル再現可能)
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

PROMPT_VERSION = "nf-v3"

# 層→モデルIDの対応 (CLAUDE.md 第14条)
MODELS: dict[str, str] = {
    "L1": "claude-haiku-4-5",
    "L2": "claude-fable-5",
}


class Tier(Enum):
    NONE = "NONE"                    # エスカレーション不要 (= エントリーしない)
    L1_ONLY = "L1_ONLY"
    L2_AFTER_L1 = "L2_AFTER_L1"


class JudgementKind(str, Enum):
    ENTRY_GATE = "ENTRY_GATE"
    WAVE_DISAMBIGUATION = "WAVE_DISAMBIGUATION"
    FUTURE_CONFLUENCE_REVIEW = "FUTURE_CONFLUENCE_REVIEW"


class JudgementRequest(BaseModel):
    """LLMへ渡す判断要求。features は事前計算済みの数値特徴量のみ
    (生ローソク足を渡さない: CLAUDE.md 第13条)。"""

    model_config = ConfigDict(frozen=True)

    kind: JudgementKind
    symbol: str
    direction: int
    features: dict                   # 数値/文字列のみ。Decimalはstr化して格納すること
    prompt_version: str = PROMPT_VERSION


class Verdict(BaseModel):
    """LLM判定結果 (structured outputs で強制するスキーマ)。"""

    model_config = ConfigDict(frozen=True)

    decision: Literal["GO", "NO_GO", "WAIT"]
    confidence: Decimal = Field(ge=0, le=1)
    reasons: list[str] = Field(default_factory=list, max_length=3)
    invalidation_price: int | None = None     # 「このシナリオが死ぬ価格」の数値化
    selected_wave_count: int | None = None    # カウント候補indexの裁定
    source: str = "LLM"                       # "L1" | "L2" | "CACHE" | "POLICY" | "GUARDRAIL"


NO_GO_POLICY = Verdict(decision="NO_GO", confidence=Decimal(1),
                       reasons=["below escalation threshold"], source="POLICY")


def cache_key(request: JudgementRequest, tier: str) -> str:
    """決定論的キャッシュキー: (model, prompt_version, feature_hash)。

    json.dumps は sort_keys=True 固定 (CLAUDE.md 第15条: 決定論化)。
    """
    payload = {
        "model": MODELS[tier],
        "prompt_version": request.prompt_version,
        "kind": request.kind.value,
        "symbol": request.symbol,
        "direction": request.direction,
        "features": request.features,
    }
    blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Verdict Cache (SQLite) — 同一特徴量への再ジャッジを無料化 (設計書 §7.4)
# ---------------------------------------------------------------------------

class VerdictCache:
    """key = cache_key(request, tier)。バックテスト再実行をほぼ無料にする。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS verdicts ("
            " cache_key TEXT PRIMARY KEY,"
            " verdict_json TEXT NOT NULL,"
            " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        self._conn.commit()

    def get(self, key: str) -> Verdict | None:
        row = self._conn.execute(
            "SELECT verdict_json FROM verdicts WHERE cache_key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        cached = Verdict.model_validate_json(row[0])
        return cached.model_copy(update={"source": "CACHE"})

    def put(self, key: str, verdict: Verdict) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO verdicts (cache_key, verdict_json) VALUES (?, ?)",
            (key, verdict.model_dump_json()),
        )
        self._conn.commit()

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# エスカレーションポリシー (決定論)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EscalationPolicy:
    """設計書 §7.1 のポリシー関数のパラメータ。"""

    score_l1: Decimal                 # S1: これ未満は LLM 不要 (=エントリーなし)
    score_l2: Decimal                 # S2: これ以上は L2 最終ジャッジ必須
    ambiguity_gray: Decimal           # エリオット曖昧度がこれ未満 (候補拮抗) なら L2
    l2_daily_call_cap: int            # L2 の1日あたり呼び出し上限 (予算)

    def decide(self, cluster_score: Decimal, ambiguity: Decimal) -> Tier:
        if cluster_score < self.score_l1:
            return Tier.NONE
        if cluster_score >= self.score_l2 or ambiguity < self.ambiguity_gray:
            return Tier.L2_AFTER_L1
        return Tier.L1_ONLY


# ---------------------------------------------------------------------------
# LLM クライアント抽象 + Anthropic 実装
# ---------------------------------------------------------------------------

class LlmClient(Protocol):
    """tier ("L1"|"L2") に応じたモデルで判定する。失敗時は例外を送出してよい
    (ガードレールは Gateway 側が一元的に担う)。"""

    def judge(self, request: JudgementRequest, tier: str) -> Verdict: ...


class AnthropicLlmClient:
    """anthropic SDK 実装 (ライブ用。CIでは FakeClient を使用)。

    - システムプロンプト (手法マニュアル+判定ルール) は凍結し
      cache_control でプロンプトキャッシュ (CLAUDE.md 第15条)
    - L2 (claude-fable-5): adaptive thinking + effort high。
      sampling パラメータと明示 thinking disabled は 400 のため渡さない (第14条)
    - structured outputs (messages.parse) で Verdict スキーマを強制
    """

    def __init__(self, system_prompt: str, *, timeout_s: float = 30.0) -> None:
        self._system = system_prompt
        self._timeout = timeout_s
        self._client = None  # 遅延初期化

    def _ensure_client(self):
        if self._client is None:
            import anthropic  # 遅延 import (バックテスト/CI環境で不要)
            self._client = anthropic.Anthropic(timeout=self._timeout)
        return self._client

    def judge(self, request: JudgementRequest, tier: str) -> Verdict:
        client = self._ensure_client()
        kwargs: dict = {}
        if tier == "L2":
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": "high"}
        response = client.messages.parse(
            model=MODELS[tier],
            max_tokens=2048,
            system=[{
                "type": "text",
                "text": self._system,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{
                "role": "user",
                "content": json.dumps(
                    {"kind": request.kind.value, "symbol": request.symbol,
                     "direction": request.direction, "features": request.features},
                    sort_keys=True, default=str, ensure_ascii=False),
            }],
            output_format=Verdict,
            **kwargs,
        )
        verdict: Verdict = response.parsed_output
        return verdict.model_copy(update={"source": tier})


# ---------------------------------------------------------------------------
# Gateway 本体
# ---------------------------------------------------------------------------

def _guardrail(reason: str) -> Verdict:
    """DEFAULT NO-TRADE (CLAUDE.md 第1条)。キャッシュには保存しない
    (一過性の障害を恒久判定として固定しないため)。"""
    return Verdict(decision="NO_GO", confidence=Decimal(1),
                   reasons=[reason], source="GUARDRAIL")


class AiGateway:
    """L0からの判断要求を受け、キャッシュ→L1→L2 の順で解決する。

    どの経路でも必ず Verdict を返し、例外を上位へ伝播させない。
    """

    def __init__(self, *, client: LlmClient, cache: VerdictCache,
                 policy: EscalationPolicy) -> None:
        self._client = client
        self._cache = cache
        self._policy = policy
        self._l2_calls_today = 0
        # 判定の集計 (CLAUDE.md 第11条: 沈黙する判断を作らない)。
        # stats: "source:decision" 別件数 / guardrail_reasons: ガードレール理由別件数
        self.stats: Counter[str] = Counter()
        self.guardrail_reasons: Counter[str] = Counter()

    @property
    def l2_calls_today(self) -> int:
        return self._l2_calls_today

    def new_day(self) -> None:
        self._l2_calls_today = 0

    # -- 判定フロー -----------------------------------------------------------

    def _note(self, verdict: Verdict) -> Verdict:
        """最終Verdictを集計してから返す (全return経路がここを通る)。"""
        self.stats[f"{verdict.source}:{verdict.decision}"] += 1
        if verdict.source == "GUARDRAIL":
            self.guardrail_reasons[verdict.reasons[0] if verdict.reasons else "?"] += 1
        return verdict

    def judge(self, request: JudgementRequest, *,
              cluster_score: Decimal, ambiguity: Decimal) -> Verdict:
        tier = self._policy.decide(cluster_score, ambiguity)
        if tier is Tier.NONE:
            return self._note(NO_GO_POLICY)

        if tier is Tier.L2_AFTER_L1:
            # 予算チェックは最初に行う (L2に到達できないならL1呼び出しも無駄)
            l2_key = cache_key(request, "L2")
            cached_l2 = self._cache.get(l2_key)
            if cached_l2 is not None:
                return self._note(cached_l2)
            if self._l2_calls_today >= self._policy.l2_daily_call_cap:
                return self._note(_guardrail("L2_BUDGET_EXHAUSTED"))

            l1 = self._resolve("L1", request)
            if l1.decision != "GO":
                return self._note(l1)            # L1却下 → L2は呼ばない (コスト最適化)

            self._l2_calls_today += 1
            return self._note(self._resolve("L2", request))

        return self._note(self._resolve("L1", request))

    def _resolve(self, tier: str, request: JudgementRequest) -> Verdict:
        """キャッシュ → LLM。失敗はガードレール (非キャッシュ) で吸収。"""
        key = cache_key(request, tier)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        try:
            verdict = self._client.judge(request, tier)
        except Exception as e:                    # noqa: BLE001 — 全障害をNO_GOへ
            return _guardrail(f"{tier}_FAILURE: {type(e).__name__}")
        verdict = verdict.model_copy(update={"source": tier})
        self._cache.put(key, verdict)
        return verdict

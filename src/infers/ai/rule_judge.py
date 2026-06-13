"""決定論ルールゲート — narrow_focus_v3.md §5 のPython実装 (LLM不使用・$0)。

§5 の NO_GO 4条件はいずれも `features` (L0が事前計算済みの数値) に対する
四則演算と閾値比較のみで判定できる (LLMはチャートを見ておらず、同じ数字
しか参照していないため)。本モジュールはその比較をそのまま実装し、
`AiGateway` の `LlmClient` プロトコルへの差し替え用クライアントを提供する。

- 失敗・例外・予算管理は不要 (純粋関数・常時$0)。
- キャッシュ・エスカレーションポリシー (L1/L2 tier) はそのまま機能するが、
  どの tier でも同じ判定を返す (コスト最適化上は無害)。
"""

from __future__ import annotations

from decimal import Decimal

from infers.ai.gateway import JudgementRequest, Verdict

# §3.3: RSI極値圏のしきい値 (Wilder RSI 14)
RSI_OVERSOLD = Decimal(30)
RSI_OVERBOUGHT = Decimal(70)

# §3.4 / §5: ambiguity が「波カウント一意」と見なせるしきい値
AMBIGUITY_HIGH = Decimal("0.3")

# §3.5 / §5: cluster_score が「厚い合流」と見なせるしきい値
CLUSTER_SCORE_STRONG = Decimal(3)

# §3.6: eta_bars の幅が「狭い」と見なせる上限 (本数)
ETA_WINDOW_NARROW = 3

# §5-1: risk = |limit - invalidation| が「極端に小さい」とみなすしきい値。
# CLAUDE.md の「第1波高値超え」と同型の max(α_atr×ATR, n_ticks) パターン。
RISK_FLOOR_ATR_FACTOR = Decimal("0.3")
RISK_FLOOR_MIN_TICKS = Decimal(1)

# §5-2: reward_ref が risk のこの比率未満なら RR劣後でNO_GO
RR_MIN_RATIO = Decimal("0.5")


def _parse_band(s: str) -> tuple[Decimal, Decimal]:
    lo, hi = s.split("..")
    return Decimal(lo), Decimal(hi)


def _parse_window(s: str) -> tuple[int, int]:
    lo, hi = s.split("-")
    return int(lo), int(hi)


def _no_go(reason: str, *, invalidation_price: int | None = None) -> Verdict:
    return Verdict(decision="NO_GO", confidence=Decimal("0.8"), reasons=[reason],
                   invalidation_price=invalidation_price, source="RULE")


def judge_features(direction: int, features: dict) -> Verdict:
    """narrow_focus_v3.md §5 の判定基準をそのまま実装した決定論判定。"""
    dow_state = features["dow_state"]
    limit = int(features["limit"])
    invalidation = int(features["invalidation"])
    w1_high = int(features["w1_high"])
    rsi_lo, rsi_hi = _parse_band(features["rsi_band"])
    eta_lo, eta_hi = _parse_window(features["eta_bars"])
    ambiguity = Decimal(features["ambiguity"])
    cluster_score = Decimal(features["cluster_score"])
    families = set(features["families"].split(","))
    atr = Decimal(features.get("atr", "0"))

    # --- §5-4: データ異常 -----------------------------------------------
    if (dow_state == "UP" and direction != 1) or (dow_state == "DOWN" and direction != -1):
        return _no_go(f"dow_state={dow_state} と direction={direction} が不整合")
    if direction > 0 and invalidation >= limit:
        return _no_go(f"invalidation={invalidation} が limit={limit} の利益方向にある(買い)",
                       invalidation_price=invalidation)
    if direction < 0 and invalidation <= limit:
        return _no_go(f"invalidation={invalidation} が limit={limit} の利益方向にある(売り)",
                       invalidation_price=invalidation)

    risk = Decimal(abs(limit - invalidation))
    reward_ref = Decimal(abs(w1_high - limit))

    # --- §5-1: 構造的脆弱性 -----------------------------------------------
    risk_floor = max(RISK_FLOOR_ATR_FACTOR * atr, RISK_FLOOR_MIN_TICKS)
    if risk <= risk_floor:
        return _no_go(f"risk={risk} <= risk_floor={risk_floor} でSLが構造的に機能しない",
                       invalidation_price=invalidation)

    # --- §5-2: リスクリワード劣後 -------------------------------------------
    if reward_ref < risk * RR_MIN_RATIO:
        return _no_go(
            f"reward_ref={reward_ref} < risk={risk}*{RR_MIN_RATIO} でRR劣後",
            invalidation_price=invalidation)

    # --- §5-3: 空のコンフルエンス -------------------------------------------
    if direction > 0:
        rsi_certain = rsi_hi <= RSI_OVERSOLD
        rsi_possible = rsi_lo <= RSI_OVERSOLD < rsi_hi
    else:
        rsi_certain = rsi_lo >= RSI_OVERBOUGHT
        rsi_possible = rsi_lo < RSI_OVERBOUGHT <= rsi_hi
    rsi_extreme = rsi_certain or rsi_possible
    has_sma = "SMA" in families
    if not rsi_extreme and not has_sma:
        return _no_go(
            f"rsi_band={features['rsi_band']} が極値圏外で families にSMAも無く"
            " 中核根拠が実体を伴わない", invalidation_price=invalidation)

    # --- GO: confidence は §5 の加点方式を再現 -------------------------------
    confidence = Decimal("0.5")
    reasons: list[str] = []
    if rsi_certain:
        confidence += Decimal("0.15")
        reasons.append(f"rsi_band={features['rsi_band']} で到達時のRSIが確実圏")
    elif has_sma:
        confidence += Decimal("0.05")
        reasons.append(f"families={features['families']} のSMA主導合流")
    else:
        reasons.append(f"rsi_band={features['rsi_band']} は到達タイミング次第で極値圏")

    if reward_ref >= risk:
        confidence += Decimal("0.1")
        reasons.append(f"reward_ref={reward_ref} >= risk={risk}")
    if ambiguity >= AMBIGUITY_HIGH:
        confidence += Decimal("0.05")
        reasons.append(f"ambiguity={ambiguity} で波カウント一意")
    if cluster_score >= CLUSTER_SCORE_STRONG:
        confidence += Decimal("0.05")
    if eta_hi - eta_lo <= ETA_WINDOW_NARROW:
        confidence += Decimal("0.05")

    confidence = min(confidence, Decimal("0.9"))
    return Verdict(decision="GO", confidence=confidence, reasons=reasons[:3],
                   invalidation_price=invalidation, source="RULE")


class RuleBasedLlmClient:
    """`LlmClient` プロトコルのドロップイン実装。LLMを一切呼ばない。

    `judge_features` を呼ぶだけの純粋関数なので、例外・タイムアウト・
    予算上限の管理は不要 (常に即時・$0)。tier ("L1"/"L2") に関わらず
    同じ判定を返す。
    """

    def judge(self, request: JudgementRequest, tier: str) -> Verdict:  # noqa: ARG002
        return judge_features(request.direction, request.features)

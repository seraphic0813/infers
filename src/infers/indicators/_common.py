"""インジケーター共通の量子化・平滑化ユーティリティ (設計書 §4.1〜4.2)。"""

from __future__ import annotations

from decimal import Decimal

# 固定量子化単位。すべての導出値はこの粒度に丸める(決定論性の要)。
Q = Decimal("0.000000001")  # 1e-9


def wilder_smooth(prev: Decimal, x: Decimal, period: int) -> Decimal:
    """Wilder平滑 1ステップ: avg' = (avg*(n-1) + x) / n を固定量子化で。"""
    return ((prev * (period - 1) + x) / period).quantize(Q)

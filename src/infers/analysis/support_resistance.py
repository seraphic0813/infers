"""レジサポゾーン検出 (設計書 §4.3 / マニュアル 3.3)。

直近の確定スイング (ZigZag出力) を価格でクラスタリングし、
幅 ε_zone = α_zone × ATR を持つ水平ゾーンとして定義する。
ラインを「点」ではなく「帯」で扱うのは、約定の現実と
コンフルエンス判定の安定性のため。

強度 = Σ(タッチ重み × 経過時間減衰)。新しいタッチほど重い。
役割 (SUPPORT/RESISTANCE) は参照価格との位置関係で決まる。
ブレイク役割転換 (flip) の逐次追跡はフェーズ5で SRZoneTracker として拡張する。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal, Sequence

from infers.analysis.indicators import Q
from infers.analysis.zigzag import SwingPoint

# 既定パラメータ (バックテストで調整)
ZONE_WIDTH_ATR = Decimal("0.5")    # ゾーン幅 ε = α × ATR
RECENCY_DECAY = Decimal("0.9")     # 1スイング古くなるごとの強度減衰


@dataclass(frozen=True)
class SRZone:
    """水平レジサポゾーン (整数ティックの閉区間)。"""

    low_int: int
    high_int: int
    touches: int                                  # 構成スイング数
    strength: Decimal                             # 減衰加重タッチ強度
    role: Literal["SUPPORT", "RESISTANCE"]        # 参照価格に対する役割

    def contains(self, price_int: int) -> bool:
        return self.low_int <= price_int <= self.high_int

    @property
    def zone(self) -> tuple[int, int]:
        return (self.low_int, self.high_int)


def build_zones(
    swings: Sequence[SwingPoint],
    atr: Decimal,
    ref_price_int: int,
    *,
    zone_width_atr: Decimal = ZONE_WIDTH_ATR,
    decay: Decimal = RECENCY_DECAY,
) -> list[SRZone]:
    """確定スイング列からレジサポゾーンを構築する (純粋関数)。

    - swings: 古い順の確定スイング (HIGH/LOW 混在でよい。レジスタンスと
      サポートはブレイクで役割転換するため、高値・安値を区別せず
      同一価格帯なら1つのゾーンに併合する)
    - atr: 現在のATR (ティック単位 Decimal, 正)
    - ref_price_int: 役割判定の参照価格 (通常は現在の終値)
    """
    if atr <= 0:
        raise ValueError("atr must be positive")
    eps = int((zone_width_atr * atr).to_integral_value(rounding=ROUND_HALF_EVEN))
    if eps < 1:
        eps = 1

    n = len(swings)
    if n == 0:
        return []

    # (価格, 新しさ順位) — 新しいスイングほど rank=0 に近い
    pts = sorted(
        ((sp.price_int, n - 1 - i) for i, sp in enumerate(swings)),
        key=lambda t: t[0],
    )

    zones: list[SRZone] = []
    cluster: list[tuple[int, int]] = []

    def flush() -> None:
        if not cluster:
            return
        prices = [p for p, _ in cluster]
        low, high = min(prices), max(prices)
        # 幅が ε に満たない場合は対称に拡張 (帯としての最低幅を保証)
        if high - low < eps:
            pad = (eps - (high - low) + 1) // 2
            low, high = low - pad, high + pad
        strength = sum((decay ** rank for _, rank in cluster), Decimal(0)).quantize(Q)
        center = (low + high) // 2
        role: Literal["SUPPORT", "RESISTANCE"] = (
            "SUPPORT" if center <= ref_price_int else "RESISTANCE"
        )
        zones.append(SRZone(
            low_int=low, high_int=high,
            touches=len(cluster), strength=strength, role=role,
        ))

    for price, rank in pts:
        if cluster and price - cluster[-1][0] > eps:
            flush()
            cluster = []
        cluster.append((price, rank))
    flush()

    # 強度の高い順 (コンフルエンス層が重み付けに使う)
    zones.sort(key=lambda z: z.strength, reverse=True)
    return zones

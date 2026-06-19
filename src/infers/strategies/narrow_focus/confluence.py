"""コンフルエンス統合 (設計書 §4.4 / マニュアル 3.4)。

各分析モジュールの判定結果を Evidence (根拠オブジェクト) に統一し、
「異なる family の根拠が 2 つ以上、同一価格ゾーンで交差する」場合のみ
ConfluenceCluster を生成する。

厳格化 (CLAUDE.md 第5条):
  - 単一根拠ではクラスタ不成立 (単一指標エントリーの構造的禁止)
  - family の判定に時間足は含めない: M5 の RSI30 と M15 の RSI30 は
    同一 family = 1 根拠と数える
  - スコアも family ごとに最強の1件のみ採用 (同種根拠の重複でスコアが
    水増しされない)
  - 買い根拠と売り根拠は決して同一クラスタに混在しない
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from infers.indicators import Q
from infers.core.models import Timeframe


class Family(Enum):
    """根拠ファミリー。≥2 の判定は family 単位 (時間足を含めない)。"""

    ELLIOTT = "ELLIOTT"
    DOW = "DOW"
    GRANVILLE = "GRANVILLE"
    RSI = "RSI"
    SR = "SR"
    FIB = "FIB"


# 時間足係数 τ: 上位足の根拠ほど重い (設計書 §4.4)
TF_COEF: dict[Timeframe, Decimal] = {
    Timeframe.M5: Decimal("1.0"),
    Timeframe.M15: Decimal("1.2"),
    Timeframe.H1: Decimal("1.5"),
    Timeframe.H4: Decimal("2.0"),
    Timeframe.D1: Decimal("3.0"),
    Timeframe.W1: Decimal("3.5"),
}


@dataclass(frozen=True)
class Evidence:
    """単一の根拠。価格は「点」ではなく帯 (zone) で持つ。"""

    family: Family
    source: str                      # 例 "GRANVILLE_BUY3_SMA90_H1" (ジャーナル用)
    direction: int                   # +1 買い / -1 売り
    tf: Timeframe
    zone: tuple[int, int]            # 有効価格帯 (整数ティック, low <= high)
    weight: Decimal                  # 基礎重み (既定 1)
    valid_until: datetime | None = None

    def __post_init__(self) -> None:
        if self.direction not in (+1, -1):
            raise ValueError("direction must be +1 or -1")
        if self.zone[0] > self.zone[1]:
            raise ValueError(f"invalid zone: {self.zone}")
        if self.weight <= 0:
            raise ValueError("weight must be positive")

    @property
    def scored_weight(self) -> Decimal:
        """weight × τ(tf)。クラスタスコアへの寄与。"""
        return (self.weight * TF_COEF[self.tf]).quantize(Q)


@dataclass(frozen=True)
class ConfluenceCluster:
    """根拠 ≥2 family が交差した価格帯 (打診エントリー候補の単位)。"""

    zone: tuple[int, int]            # 全根拠ゾーンの交差区間
    direction: int
    evidences: tuple[Evidence, ...]
    distinct_families: int           # ★ 常に >= 2 (構築時に保証)
    score: Decimal                   # family ごとの最強根拠の scored_weight 合計

    def contains(self, price_int: int) -> bool:
        return self.zone[0] <= price_int <= self.zone[1]


def _score_cluster(evidences: list[Evidence]) -> Decimal:
    """family ごとに最強の1件のみ採用して合算 (同種重複の水増し防止)。"""
    best: dict[Family, Decimal] = {}
    for ev in evidences:
        w = ev.scored_weight
        if ev.family not in best or w > best[ev.family]:
            best[ev.family] = w
    return sum(best.values(), Decimal(0)).quantize(Q)


def find_clusters(
    evidences: list[Evidence] | tuple[Evidence, ...],
    *,
    min_families: int = 2,
    now: datetime | None = None,
) -> list[ConfluenceCluster]:
    """Evidence 集合からコンフルエンスクラスタを抽出する (純粋関数)。

    アルゴリズム: 方向ごとに zone.low でソートし、走査しながら
    「共通交差区間」が空にならない限り同一クラスタへ併合する
    (クラスタ内の全根拠はペアワイズに共通の価格帯を持つことが保証される)。

    min_families 未満のグループは破棄する (既定2 = マニュアル3.4 の
    絶対条件。1 への緩和は CLAUDE.md 第5条違反であり許可されない)。
    """
    if min_families < 2:
        raise ValueError("min_families must be >= 2 (CLAUDE.md rule 5)")

    active = [
        ev for ev in evidences
        if now is None or ev.valid_until is None or ev.valid_until > now
    ]

    clusters: list[ConfluenceCluster] = []
    for direction in (+1, -1):
        group = sorted(
            (ev for ev in active if ev.direction == direction),
            key=lambda e: e.zone,
        )
        members: list[Evidence] = []
        ilow = ihigh = 0

        def flush() -> None:
            families = {ev.family for ev in members}
            if len(families) >= min_families:
                clusters.append(ConfluenceCluster(
                    zone=(ilow, ihigh),
                    direction=direction,
                    evidences=tuple(members),
                    distinct_families=len(families),
                    score=_score_cluster(members),
                ))

        for ev in group:
            if not members:
                members = [ev]
                ilow, ihigh = ev.zone
                continue
            if ev.zone[0] <= ihigh:
                # 共通交差区間が残る → 併合
                members.append(ev)
                ilow = max(ilow, ev.zone[0])
                ihigh = min(ihigh, ev.zone[1])
            else:
                flush()
                members = [ev]
                ilow, ihigh = ev.zone
        flush()

    clusters.sort(key=lambda c: c.score, reverse=True)
    return clusters

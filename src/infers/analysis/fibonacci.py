"""フィボナッチ目標値の投影 (設計書 §3.4 / マニュアル 2.3)。

- 第3波の到達目標: 第1波の値幅 (整数ティック) を 100% とし、
  第2波の終点に 100% / 161.8% / 261.8% を投影する。
- 第5波の到達目標: 同じ第1波の値幅を第4波の終点に投影する。

計算は int (ティック) × Decimal 比率のみ。結果価格は ROUND_HALF_EVEN で
整数ティックへ量子化する (SymbolSpec.to_ticks と同一の丸め規約)。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal

from infers.analysis.elliot import WaveCount
from infers.analysis.zigzag import SwingPoint

# マニュアル 2.3 指定の投影比率
RATIOS: tuple[tuple[str, Decimal], ...] = (
    ("100.0", Decimal("1.000")),
    ("161.8", Decimal("1.618")),
    ("261.8", Decimal("2.618")),
)


@dataclass(frozen=True)
class FibTarget:
    """波の到達目標 (設計書 §3.4)。Evidence と利確目標の両方に供給される。"""

    target_wave: int                    # 3 または 5
    direction: int                      # +1 上昇 / -1 下降
    base_len: int                       # 第1波の値幅 (正のティック数)
    anchor: SwingPoint                  # 投影の起点 (第2波終点 or 第4波終点)
    levels: dict[str, int]              # {"100.0": price_int, "161.8": ..., "261.8": ...}


def _project(anchor_price: int, direction: int, base_len: int) -> dict[str, int]:
    levels: dict[str, int] = {}
    for label, ratio in RATIOS:
        offset = (Decimal(base_len) * ratio).to_integral_value(rounding=ROUND_HALF_EVEN)
        levels[label] = anchor_price + direction * int(offset)
    return levels


def project_wave3(wc: WaveCount) -> FibTarget | None:
    """第3波の最大到達目標。第2波終点 (P2) が確定していなければ None。"""
    if len(wc.pivots) < 3:
        return None
    base_len = wc.wave_len(1)
    anchor = wc.pivots[2]
    return FibTarget(
        target_wave=3,
        direction=wc.direction,
        base_len=base_len,
        anchor=anchor,
        levels=_project(anchor.price_int, wc.direction, base_len),
    )


def project_wave5(wc: WaveCount) -> FibTarget | None:
    """第5波の到達目標。第4波終点 (P4) が確定していなければ None。"""
    if len(wc.pivots) < 5:
        return None
    base_len = wc.wave_len(1)
    anchor = wc.pivots[4]
    return FibTarget(
        target_wave=5,
        direction=wc.direction,
        base_len=base_len,
        anchor=anchor,
        levels=_project(anchor.price_int, wc.direction, base_len),
    )


def project(wc: WaveCount) -> list[FibTarget]:
    """カウントの進行度に応じて算出可能な目標をすべて返す。"""
    targets = []
    for fn in (project_wave3, project_wave5):
        t = fn(wc)
        if t is not None:
            targets.append(t)
    return targets

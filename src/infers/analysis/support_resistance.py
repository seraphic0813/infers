"""後方互換シム (改善プラン T14)。

水平レジサポ・ゾーン判定の実体は [strategies/narrow_focus/support_resistance.py](
../strategies/narrow_focus/support_resistance.py) (L2) へ移設された(本モジュールも
dow.py 同様、実際には `infers.strategies.narrow_focus.zigzag.SwingPoint` を参照する
L2→L1 の逆向き依存を抱えた Narrow Focus 固有ロジックだった)。

旧来の `from infers.analysis.support_resistance import SRZone, ...` を壊さないため
本モジュールが再公開する(PEP 562: 属性アクセス時に遅延 import する)。
"""

from __future__ import annotations

_LAZY_FROM_L2 = {"SRZone", "build_zones", "ZONE_WIDTH_ATR", "RECENCY_DECAY"}


def __getattr__(name: str):
    if name in _LAZY_FROM_L2:
        from infers.strategies.narrow_focus import support_resistance
        return getattr(support_resistance, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

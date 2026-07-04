"""後方互換シム (改善プラン T14)。

ダウ理論状態機械の実体は [strategies/narrow_focus/dow.py](
../strategies/narrow_focus/dow.py) (L2) へ移設された(本モジュールは
Narrow Focus 固有の分析ロジックであり L1 汎用部品ではないため。実際、
移設前から `infers.strategies.narrow_focus.zigzag.SwingPoint` を参照する
L2→L1 の逆向き依存を抱えていた)。

旧来の `from infers.analysis.dow import StructureEvent, ...` を壊さないため
本モジュールが再公開する(PEP 562: 属性アクセス時に遅延 import する)。
"""

from __future__ import annotations

_LAZY_FROM_L2 = {
    "StructureEventType", "TrendState", "classify_dow", "StructureEvent",
    "DowStateMachine", "DowFamily",
}


def __getattr__(name: str):
    if name in _LAZY_FROM_L2:
        from infers.strategies.narrow_focus import dow
        return getattr(dow, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

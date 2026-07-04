"""後方互換パッケージ (改善プラン T14)。

dow.py / support_resistance.py は Narrow Focus 固有の分析ロジックであり
(内部で `infers.strategies.narrow_focus.zigzag.SwingPoint` を参照する
L2 依存を実際には抱えていた)、[strategies/narrow_focus/](
../strategies/narrow_focus/) (L2) へ移設済み。本パッケージは後方互換の
再公開のみを行う(`dow.py`/`support_resistance.py` はそれぞれ PEP 562 の
遅延 re-export シム)。新規コードは `infers.strategies.narrow_focus.dow` /
`.support_resistance` を直接 import すること。
"""

from infers.strategies.narrow_focus.dow import (
    DowStateMachine, StructureEvent, StructureEventType, TrendState, classify_dow,
)
from infers.strategies.narrow_focus.support_resistance import SRZone, build_zones
from infers.indicators import ATR, SMA, RsiState, WilderRSI, rsi_forward, rsi_value

__all__ = [
    "ATR", "SMA", "WilderRSI", "RsiState", "rsi_forward", "rsi_value",
    "DowStateMachine", "StructureEvent", "StructureEventType", "TrendState", "classify_dow",
    "SRZone", "build_zones",
]

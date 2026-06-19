"""共有ダウ構造・レジサポ型 (L0/L2 双方から参照される過渡的な共有モジュール)。

dow.py / support_resistance.py は core/loop.py・execution/sm.py(L0)から
StructureEvent/SRZone 型を直接参照されるため、手法層(strategies/)へは
まだ完全移動していない(docs/phase2-architecture.md §8 既知の課題。
ExecutionModel抽象化(段階2.3)で解消予定)。それ以外のNarrow Focus固有の
分析モジュール(zigzag/elliot/fibonacci/micro/future_discretion/confluence)
は strategies/narrow_focus/ へ移動済み。
"""

from infers.analysis.dow import (
    DowStateMachine, StructureEvent, StructureEventType, TrendState, classify_dow,
)
from infers.analysis.support_resistance import SRZone, build_zones
from infers.indicators import ATR, SMA, RsiState, WilderRSI, rsi_forward, rsi_value

__all__ = [
    "ATR", "SMA", "WilderRSI", "RsiState", "rsi_forward", "rsi_value",
    "DowStateMachine", "StructureEvent", "StructureEventType", "TrendState", "classify_dow",
    "SRZone", "build_zones",
]

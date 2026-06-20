"""執行層: 安全境界・Simブローカー・リスクマネージャー (設計書 §6)。

Narrow Focus 執行 FSM (FsmConfig / PosState / NarrowFocusExecution) の実体は
L2 (`strategies/narrow_focus/execution.py`) へ移設された (段階2.3b)。本パッケージ
(L0) は L2 をモジュールレベルで import しないため、それらは遅延 re-export する。
"""

from infers.core.execution import SlMonotonicityError, TransitionError
from infers.execution.risk import OrderRequest, RiskConfig, RiskManager, RiskVerdict
from infers.execution.sim_broker import BrokerEvent, BrokerRejection, SimBroker

# 手法固有の執行 FSM (L2) は属性アクセス時に遅延 import (PEP 562)。
_LAZY_FROM_L2 = {"FsmConfig", "PosState", "NarrowFocusExecution", "PositionFSM"}


def __getattr__(name: str):
    if name in _LAZY_FROM_L2:
        from infers.strategies.narrow_focus import execution
        return getattr(execution, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "FsmConfig", "NarrowFocusExecution", "PositionFSM", "PosState",
    "SlMonotonicityError", "TransitionError",
    "SimBroker", "BrokerEvent", "BrokerRejection",
    "RiskManager", "RiskConfig", "RiskVerdict", "OrderRequest",
]

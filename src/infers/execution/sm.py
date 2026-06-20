"""後方互換シム (段階2.3b)。

Narrow Focus 執行状態機械の実体は [strategies/narrow_focus/execution.py](
../strategies/narrow_focus/execution.py) (L2) へ移設された。汎用の安全境界
(`BrokerPort`)・SL単調性ガード (`SlMonotonicityError`)・FSM エラー
(`TransitionError`) は [core/execution.py](../core/execution.py) (L0) へ移設された。

旧来の `from infers.execution.sm import FsmConfig, PositionFSM, PosState, ...` を
壊さないため本モジュールが再公開する。L0 層に属するこのモジュールが L2 を
モジュールレベルで import しないよう、手法固有シンボル (FsmConfig / PosState /
NarrowFocusExecution / PositionFSM) は属性アクセス時に遅延 import する (PEP 562)。
"""

from __future__ import annotations

# 汎用 (L0) — そのまま再公開。
from infers.core.execution import (  # noqa: F401
    BrokerPort, SlMonotonicityError, TransitionError,
)

# 手法固有 (L2) — 遅延 re-export (L0→L2 のモジュールレベル依存を避ける)。
_LAZY_FROM_L2 = {"FsmConfig", "PosState", "NarrowFocusExecution", "PositionFSM"}


def __getattr__(name: str):
    if name in _LAZY_FROM_L2:
        from infers.strategies.narrow_focus import execution
        return getattr(execution, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

"""モード非依存の取引コア (CLAUDE.md 第12条)。

loop.py は infers.analysis.dow (L0/L2 共有の過渡的モジュール) に依存し、
それが strategies.narrow_focus.zigzag 経由で infers.core.models を参照する
ため、ここで loop を即時 import すると循環 import になる。models のみ
re-export し、loop は `infers.core.loop` を直接 import すること。
"""

from infers.core.models import Candle, SymbolSpec, Timeframe

__all__ = ["Candle", "SymbolSpec", "Timeframe"]

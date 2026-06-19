"""インジケーター層 (L1): 汎用テクニカル指標 (設計書 §4.1〜4.2 / CLAUDE.md 第6条)。

- 入力価格はすべて整数ティック (int)。float は受け取らない。
- 導出値 (SMA/ATR/RSI) は固定量子化 Decimal (1e-9) で表現する。
  浮動小数を使わず、量子化を毎ステップ行うことでプラットフォーム
  非依存の決定論性 (バックテスト⇄ライブの同一性) を保証する。
- 本パッケージは手法(strategies/)に依存しない汎用部品のみを置く
  (docs/phase2-architecture.md §3)。MACD等の新規指標もここに追加する。
"""

from infers.indicators._common import Q
from infers.indicators.atr import ATR
from infers.indicators.rsi import RsiState, WilderRSI, rsi_forward, rsi_value
from infers.indicators.sma import SMA

__all__ = ["Q", "SMA", "ATR", "RsiState", "WilderRSI", "rsi_forward", "rsi_value"]

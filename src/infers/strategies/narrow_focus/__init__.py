"""Narrow Focus 手法 (depth50 ベースライン)。

エントリー判定ロジックの正は同フォルダの entry-methodology.md。本パッケージは
分析パイプライン(provider.py)+ Elliott/Fibonacci/ZigZag/RSIマルチTF/
未来裁量/コンフルエンスの各分析モジュールから構成される。

provider.py は infers.analysis.dow/support_resistance (L0/L2 共有の過渡的
モジュール) に依存し、それらは本パッケージの zigzag.py を SwingPoint 型の
ために参照する。ここで provider を即時 import すると循環 import になるため、
あえてここでは re-export しない(`infers.strategies.narrow_focus.provider`
を直接 import すること)。
"""

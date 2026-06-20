"""market_tpsl 手法 (段階2.5 / docs/phase2-architecture.md §4)。

SMAクロスで成行参入し、固定TP/SLで手仕舞う最小手法。Narrow Focus とは
まったく異なる執行ライフサイクル(打診→追撃→半利→ランナーを持たない)を
TradingLoop が同一コードパスで駆動できることを実証するための検証用手法。

循環 import を避けるため、本パッケージ __init__ は eager import を持たない
(各モジュールはフルパスで直接 import する。narrow_focus パッケージと同方針)。
"""

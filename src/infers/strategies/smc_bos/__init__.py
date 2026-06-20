"""smc_bos 手法 (段階S2 / spec.md)。

M30 SMC BOS(Break of Structure)+ EMA80 フィルタ。構造ブレイク成行参入 +
固定SL/RR利確(段階S2はbe_mode=offの最小構成。SL前進は段階S4で追加)。
Narrow Focus / market_tpsl とは別の執行ライフサイクル。

循環 import を避けるため、本パッケージ __init__ は eager import を持たない
(narrow_focus / market_tpsl と同方針)。
"""

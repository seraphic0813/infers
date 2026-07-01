"""atr_trend_scalp 手法パッケージ (L2 / spec.md)。

XAU/USD 5M ハイブリッド・ATRトレンドフォロー・スキャルピング。EMA9/21 の
ゴールデン/デッドクロス + EMA21 押し目 + ATR/出来高フィルタで参入し、15M の
EMA50 を上位足バイアスに用いる。出口は 50/50 分割 (TP1で半利+建値化、残玉は
0.5×ATR トレーリング + 2.0×ATR 最終TP)。

循環 import を避けるため、ここでは provider/execution/signals を即時 re-export
しない (既存手法パッケージと同方針)。エントリー判定ロジックの正は spec.md。
"""

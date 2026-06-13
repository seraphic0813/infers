# バックテストレポート一覧

各ディレクトリの `report.html` をブラウザで開く(同階層の `report_data.js` を読む)。
XAUUSD M5・5年・ルールベースゲート($0)・スプレッド2tick・スワップ込み。

| ディレクトリ | 構成 | トレード数 | PF | 最大DD | 週方向純度 |
|---|---|---|---|---|---|
| `rule_swap/` | **M5単独**(マクロフィルターOFF。比較用ベースライン) | 4,247 | 1.07 | $1,011 | — |
| `rule_macro_h4/` | **H4マクロ**(H4トレンドと一致時のみ発注)★最良 | 1,712 | **1.11** | **$738** | 40% |
| `rule_macro_d1/` | **D1マクロ**(週〜月の方向。週純度96%だが**損失**) | 1,607 | **0.92** | $1,527 | **96%** |

## どれを見るべきか

- **最新の戦略状態** = マクロフィルター有効版(`rule_macro_d1/` または `rule_macro_h4/`)
- `rule_swap/` は「フィルターを切った従来挙動」で、4,247件のままなのは**正しい**(比較用)

## 再生成コマンド

```
python -m infers.main --mode backtest --data data/xauusd_m5.parquet \
  --verdict-cache work/cache/verdicts_macro_d1.sqlite3 \
  --macro-tf D1 --report reports/rule_macro_d1
```

- `--macro-tf H4|D1|W1` でマクロ足を変更(方向の持続スケールが変わる)
- `--no-macro-filter` でフィルター無効(M5単独の従来挙動)

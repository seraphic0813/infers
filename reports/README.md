# バックテストレポート一覧

各ディレクトリの `report.html` をブラウザで開く(同階層の `report_data.js` を読む)。
XAUUSD M5・5年・ルールベースゲート($0)・スプレッド2tick・スワップ込み。

| ディレクトリ | 構成 | トレード数 | PF | 最大DD | 利益集中 |
|---|---|---|---|---|---|
| `rule_swap/` | M5単独(全フィルターOFF。ベースライン) | 4,247 | 1.07 | $1,011 | 上位5=117% |
| `rule_h4_nofib/` | **経験的ベスト**(H4マクロ+FIB除外) | 1,229 | **1.22** | **$557** | 上位5=117% |
| `rule_wave2_fix1/` | 手法準拠(本物の第2波+40%深さ+ランナー伸ばし) | 437 | 1.008 | $1,168 | 上位5=3824% |
| `rule_wave2_sma90tp/` | **手法準拠 最新**(+90SMA半分利確) | 437 | 1.012 | $1,156 | 上位5=3824% |

## どれを見るべきか

- **手法の執行を検証する** = `rule_wave2_sma90tp/`(本物のH4第2波・40%深さ・手法準拠の出口)
- **リスク効率で見る最良** = `rule_h4_nofib/`(が、これは「浅い押し目」を拾う非手法版)
- 手法準拠版は利益が上位5件に集中(脆い)。経験的版は分散していて相対的に頑健

## トレード詳細パネルで手法を検証(2026-06-14 追加)

各トレードを選ぶと、詳細に **方向/マクロダウ/第2波TF・コンフルエンス・押し目深さ(40%以内か)・半分利確トリガー(RSI/SMA90/SR)** が出る。チャート上は SMA90(青)/SMA200(橙)/FIB(金点線)/SR(水色破線)/無効化/第1波高値/フィボ目標 を表示。

## 再生成コマンド

```
# 手法準拠 (本物の第2波 + 40%深さ + FIB除外)
python -m infers.main --mode backtest --data data/xauusd_m5.parquet \
  --verdict-cache work/cache/verdicts_w2.sqlite3 \
  --macro-wave2 --depth-screen --no-fib-score --report reports/rule_wave2_sma90tp
```

- `--macro-wave2`(上位足エリオット第2波)/ `--depth-screen`(40%深さ)/ `--no-fib-score`(FIB除外)
- `--macro-tf H4|D1`(マクロ足)/ `--no-macro-filter`(マクロ方向フィルター無効)
- テンプレート(表示)だけの更新は `report_html._HTML_TEMPLATE` を該当 `report.html` へ write_text(再実行不要)

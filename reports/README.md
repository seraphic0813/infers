# バックテストレポート一覧

各ディレクトリの `report.html` をブラウザで開く(同階層の `report_data.js` を読む)。
XAUUSD M5・5年・ルールベースゲート($0)・スプレッド2tick・スワップ込み。

| ディレクトリ | 構成 | トレード数 | PF | 最大DD | 利益集中 |
|---|---|---|---|---|---|
| `rule_swap/` | M5単独(全フィルターOFF。ベースライン) | 4,247 | 1.07 | $1,011 | 上位5=117% |
| `rule_h4_nofib/` | **経験的ベスト**(H4マクロ+FIB除外) | 1,229 | **1.22** | **$557** | 上位5=117% |
| `rule_wave2_fix1/` | 手法準拠(本物の第2波+40%深さ+ランナー伸ばし) | 437 | 1.008 | $1,168 | 上位5=3824% |
| `rule_wave2_sma90tp/` | 手法準拠(+90SMA半分利確。G2 family化 前) | 437 | 1.012 | $1,156 | 上位5=3824% |
| `rule_g2families/` | 手法準拠(G2 全 family 詳細化: ダウ/SMAグランビル/RSIマルチTF/SR役割+相反veto) | 510 | 1.035 | $677 | 上位5=888% |
| `rule_riskfix/` | 手法準拠(+RSI M5加点を線形パス基準に厳格化、+1トレード最大リスク上限) | 570 | 1.543 | $287 | 上位5=76% |
| `rule_depth50/` | **手法準拠 確定ベースライン(v1.0)**(riskfix+40%深さを50%へ緩和。買い黒字転換・純益+9%) | 624 | 1.489 | $326 | 上位5=70% |
| `rule_depthtier2/` | 参考(リスク重視の代替案。riskfix+深さ階層化+上位足RSI/200SMA壁) | 573 | 1.561 | $266 | 上位5=74% |

## どれを見るべきか

**`rule_depth50/` を確定ベースライン(v1.0)とする**(詳細は`rule_riskfix/health_check.md`
実験1〜6)。

- `rule_depth50/`は`rule_riskfix`に対し40%深さスクリーニングを50%戻りまで緩和した構成。
  40%は原典に無いシステム独自値で過剰に厳しく買い側を機会損失させていた。買いが赤字→黒字
  転換(PF0.711→1.092)し、純益+9%・利益分散も改善(76%→70%)。**変更点は単一パラメータ
  (`depth_max`)のみで説明可能、かつ売買双方向で健全**という点を重視して採用した(実験6)。
- `rule_depthtier2/`(深さ階層化+上位足RSI/200SMA壁、PF1.561/DD$266)はPF・DDのみ見ると
  depth50を上回るが、(1)5〜6パラメータの複合機構で構成され壁の発火率は約2%と低く、改善が
  メカニズムの有効性によるものか単により厳しいフィルターの副産物かを切り分けられない
  (過剰適合リスク)、(2)買いはriskfixと完全同一(−$110.71/PF0.711)で恒常的な負け筋を
  内包したまま、という理由で確定ベースラインには採用しない。「リスクを最優先する場合の
  代替案」として参考保存する(実験3・4・6)。
- `rule_riskfix/`(`rule_g2families` に RSI M5加点の線形パス基準化 + 1トレード最大リスク上限を
  追加)は depth_max=0.40 の前段ベースライン。比較の出発点としてのみ参照する。
- depth50とdepthtier2を統合した`rule_depth50tier`も検証したが、`rule_depth50`と完全同一
  (浅い押し目帯で壁の発火がゼロのため統合効果なし)で不採用(実験5)
- 手法準拠版でも利益はなお上位数件に依存するが、`rule_g2families`(888%)から
  `rule_riskfix`(76%)→`rule_depth50`(70%)へ段階的に分散・健全化した

## G2 family 詳細化(2026-06-14)による変化: `sma90tp` → `g2families`

ゲート2の独立根拠を entry-methodology.md の詳細仕様どおりに実装(RSI/SR/SMA を「1 family=1点」
二値化 + 強度記録、相反根拠でのクラスタ破壊veto、ダウ順行の family 化と方向のマクロ起点化)。

| 指標 | sma90tp (前) | g2families (後) | 変化 |
|---|---|---|---|
| トレード数 | 437 | 510 | +16.7% |
| PF | 1.012 | **1.035** | 改善 |
| 最大DD | $1,156 | **$677** | −41%(大幅改善) |
| 利益集中(上位5) | 3824% | 888% | 脆さ低下(まだ集中) |
| 純益(5年/$10k) | — | +$95.51 (+0.96%) | 限界的 |

- **読み方**: PF・最大DD・利益分散はいずれも改善したが、純益は依然 +0.96%/5年(PF1.035)と限界的。
  低頻度・薄利の手法準拠構成の性格は変わらない(個別フィルターの寄与切り分けは未実施)。
- ダウ family は 420/510 件で成立(うち方向がマクロ起点に変わったことで生じた「ミクロ非順行でも
  発注」= dow_strength NONE が 90 件)。

## RSI厳格化+1トレード最大リスク上限(2026-06-15)による変化: `g2families` → `riskfix`

`#503` のエントリー方向誤認・`#508-510` の壊滅的損失($81-149、最大DDの大半を占有)の分析を受け、
2点修正した(詳細は entry-methodology.md の該当訂正節)。

1. **RSI M5加点の厳格化**: `m5_aligned` を「いずれかの前方パスが極値到達(`_possible`)」から
   「線形パス(中心的シナリオ)が極値到達(`_likely`)」へ変更。十分離れたセルでは `_possible` が
   ほぼ常時成立し RSI familyが実質ノーチェックになっていた問題を修正(相反veto`_conflict`は
   安全側の`_possible`を維持=非対称)。
2. **1トレード最大リスク上限**: `ProviderConfig.max_risk_ticks`(既定 10000 = $100、
   probe_volume_steps=2 なら SL距離 $50 まで)を新設。40%深さスクリーニングが選ぶ最良候補の
   SL距離が第1波規模に比例して外れ値化する(通常 $3-4 に対し $80-150)場合、リスク上限内の
   次点候補へフォールバックし、全候補が超えるなら見送り(NO-TRADE)。

| 指標 | g2families (前) | riskfix (後) | 変化 |
|---|---|---|---|
| トレード数 | 510 | 570 | +11.8% |
| PF | 1.035 | **1.543** | 大幅改善 |
| 最大DD | $677 | **$287** | −58% |
| 利益集中(上位5) | 888% | **76%** | 大幅分散 |
| 純益(5年/$10k) | +$95.51 (+0.96%) | **+$1,324.55 (+13.25%)** | 大幅改善 |
| SL距離(median/p90/max) | — | $3.98 / $15.75 / $48.90 | 外れ値解消($81-149→最大$48.90) |
| 最大損失トレード | -$299.24 | -$92.24 | 大幅縮小 |

- **読み方**: 2点とも「手法の定義(40%深さ・無効化価格・RSI極値)自体は変更せず、加点/候補選定の
  事後フィルターを絞る」設計。トレード数は減らずむしろ増加(+11.8%)しつつ、PF・最大DD・
  利益分散・純益のすべてが改善し、`rule_h4_nofib`(経験的ベスト, PF1.22/DD$557)を初めて
  手法準拠版が上回った。

## トレード詳細パネルで手法を検証(2026-06-14 追加)

各トレードを選ぶと、詳細に **方向/マクロダウ/第2波TF・コンフルエンス・押し目深さ(40%以内か)・半分利確トリガー(RSI/SMA90/SR)** が出る。チャート上は SMA90(青)/SMA200(橙)/FIB(金点線)/SR(水色破線)/無効化/第1波高値/フィボ目標 を表示。

## 再生成コマンド

```
# ベースライン1: 純益最大化 (riskfix + 40%深さを50%戻りまで緩和)
python -m infers.main --mode backtest --data data/xauusd_m5.parquet \
  --verdict-cache work/cache/verdicts_depth50.sqlite3 \
  --macro-wave2 --depth-screen --depth-max 0.50 --no-fib-score --report reports/rule_depth50

# ベースライン2: リスク調整後最良 (riskfix + 深さ階層化 + 上位足RSI/200SMA壁)
python -m infers.main --mode backtest --data data/xauusd_m5.parquet \
  --verdict-cache work/cache/verdicts_depthtier2.sqlite3 \
  --macro-wave2 --depth-screen --depth-tier --no-fib-score --report reports/rule_depthtier2

# 前段ベースライン (G2 全 family 詳細化 + RSI厳格化 + 1トレード最大リスク上限。
# 本物の第2波 + 40%深さ(depth_max=0.40既定) + FIB除外)
python -m infers.main --mode backtest --data data/xauusd_m5.parquet \
  --verdict-cache work/cache/verdicts_riskfix.sqlite3 \
  --macro-wave2 --depth-screen --no-fib-score --report reports/rule_riskfix
```

- `--macro-wave2`(上位足エリオット第2波)/ `--depth-screen`(40%深さ)/ `--no-fib-score`(FIB除外)
- `--macro-tf H4|D1`(マクロ足)/ `--no-macro-filter`(マクロ方向フィルター無効)
- テンプレート(表示)だけの更新は `report_html._HTML_TEMPLATE` を該当 `report.html` へ write_text(再実行不要)

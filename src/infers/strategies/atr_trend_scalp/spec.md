# XAU/USD M5 ハイブリッド・ATRトレンドフォロー・スキャルピング 仕様書(`atr_trend_scalp`)

> **ステータス: 初期実装完了(A0〜A3)** — 2026-07-01 起案・実装。
> INFERS プラットフォームの4つ目の手法。`narrow_focus`/`depth50`・`market_tpsl`・
> `smc_bos` とは別の執行ライフサイクル(50/50分割+建値化+トレール+段階TP)を持つ。
> **別口座での運用を想定**(コードは口座非依存。資格情報は環境変数のみ)。
>
> **本書の位置づけ(文書優先順位)**:
> - プラットフォーム全体の正は [phase2-architecture.md](../../../../docs/phase2-architecture.md)。
> - **本手法のエントリー判定ロジックの正は本書**(CLAUDE.md 配置方針: 方法論文書は
>   手法フォルダ配下に置く。`docs/` に置かない)。
> - 本書は Narrow Focus / depth50 / market_tpsl / smc_bos の挙動を**1ビットも変えない**
>   ことを前提とする(全変更は加算的)。

---

## 0. 要約(TL;DR)

- **何を足すか**: XAU/USD の M5 トレンドフォロー・スキャルピング。M15 の EMA50 を上位足
  バイアスに、M5 の EMA9/21 ゴールデン/デッドクロス + EMA21 押し目 + ATR/出来高フィルタ
  で成行参入する。高ボラティリティ環境適応型のトレンドフォロー。
- **執行の核**: 50/50分割。**TP1(1.0×ATR)で半玉利確 + 残玉SLを建値へ移動 → RUNNER**、
  残玉は **0.5×ATR トレーリング + TP2(2.0×ATR)** で手仕舞い。
- **実装**: `strategies/atr_trend_scalp/`(`resample.py`=M5→M15 / `provider.py`=4条件ゲート /
  `execution.py`=`AtrTrendExecution` FSM / `signals.py`=`AtrTrendPlan`/`AtrTrendOutput` /
  `report.py`)+ レジストリ1件登録。**L0/L1 への新規追加は不要**(M5/M15/EMA/ATR/SMA は既存)。
- **AIゲート**: Narrow Focus 固有特徴量前提の既定ルールゲートは本手法の features を
  GUARDRAIL NO_GO にするため、**`--ai-client none`(パススルー)** で参入ゲートを無効化する
  (smc_bos/market_tpsl と同じ既知制約。防御層は別途全手法に強制され続ける)。

---

## 1. 取引対象・時間軸

| 項目 | 設定 |
|---|---|
| 銘柄 | XAU/USD(ゴールド) |
| 執行TF | M5(確定足クローズ時のみロジック評価。CLAUDE.md §A-2) |
| 上位足バイアス | M15 の EMA50(provider 内部で M5 からリサンプル。`resample.py`) |
| 推奨時間帯 | ロンドン〜NY(JST16:00〜25:00 = UTC07:00〜16:00)。`session_filter` で opt-in(既定OFF) |

---

## 2. エントリー判定(本手法の正)

M5 確定足ごとに以下4条件がすべて成立し、かつ建玉ミラーがフラット
(+ `session_filter` 有効時は時間帯内)のとき、成行参入プランを1件出す。

### ロング(買建。ショートは全条件を対称に反転)
1. **上位足バイアス**: 直近確定 M15 終値 > EMA50(M15)。
2. **短期モメンタム**: EMA9 > EMA21(M5・ゴールデンクロス状態)。
3. **リトレース(押し目)**: 直近 `retrace_lookback`(既定3)本以内で安値が EMA21 を
   下回る/接触し、当足終値が EMA21 を回復(反発)している。
4. **ボラ&出来高フィルタ**: `ATR14 >= atr_vol_mult(1.1) × 直近20本平均ATR`
   **かつ** `volume >= vol_mult(1.2) × 出来高SMA20`(ダマシ排除)。

- 参入参考価格 = 確定足終値(仕様書「次足始値で成行」の代理。SimBroker は確定足
  クローズ価格で約定)。**確定足主義**によりティック/形成中バーのヒゲでは判定しない。
- **平均ATR20** は直近20本(当足含む)のATRの単純平均。**出来高**は MT5 の tick_volume
  (XAUUSD CFD に真の出来高は無い。業界標準の代替指標)。

---

## 3. 出口・防御(`AtrTrendExecution` FSM / §A 準拠)

すべて **100%決定論の Python**(LLM非依存 §A-1)。状態は Enum 一本(§A-8):

```
IDLE ─place()─▶ OPEN(全玉・初期SL) ─TP1到達─▶ RUNNER(残玉・SL=建値, 0.5×ATRトレール) ─TP2到達─▶ CLOSED
        │                                    │
        └── on_broker_event: SL_HIT ─────────┴──▶ CLOSED     close(): データ末尾/停止 ─▶ CLOSED
```

- **初期SL**: `entry ∓ sl_atr_mult(1.0)×ATR`(min_stop_distance_ticks を下限にガード)。
  発注と同時に必ず設定(SLなしの状態は作らない)。
- **TP1(第1利確)**: `entry ± 1.0×ATR` 到達で **半玉を利確** + 残玉SLを**建値(実約定価格)**
  へ移動。`volume_steps` が1で半玉が作れない場合は部分利確を行わず建値化のみ(残玉全量)。
- **トレール**: RUNNER で毎確定足、`順行側の極値 − 方向×round(0.5×ATR)` へSLを前進
  (**利益方向のみ**。エントリー時ATRで固定した幅)。
- **TP2(第2利確)**: `entry ± 2.0×ATR` 到達で残玉全決済 → CLOSED。
- 半玉数は `--risk-pct` で volume がリサイズされ得るため**プランに焼き込まず**、`place()`
  時点の実 volume_steps から `half = volume // 2` を導出する(端数は残玉側へ寄せる)。

### 含み益トリガーの採用(A-4 手法契約)
本手法は **含み益トリガーの防御調整(TP1到達で建値化、その後トレール)を自手法の契約
として採用**する。CLAUDE.md §A-4 の手法スコープ化により許可される(Narrow Focus は
この自由を使わない)。判定は PnL額ではなく**確定足の価格水準到達**で行う(§A-2 準拠。
実装上は価格比較)。全手法に残る不変条件は **A-3(SL単調性=利益方向にのみ移動)** のみで、
建値化・トレールはこれを順守(`_advance_sl_to` が改善以外を no-op で無視)。

> **既知の留意点**: smc_bos の実測では早期建値化(`at_1r`)がむしろ成績を悪化させた
> 前例がある(smc_bos/spec.md §3.3)。本手法は仕様書どおり建値化+トレールを**既定・中核**
> として実装するが、実測(段階A4)で優位性を削る可能性がある。その場合の代替(トレール
> のみ/建値化なし等)は実測後に別途検討する。

---

## 4. 守るべき不変条件(CLAUDE.md §A)

| 条項 | 扱い |
|---|---|
| A-1 防御はLLM非依存 | ✅ execution/参入ゲートとも LLM 非依存 |
| A-2 確定足主義 | ✅ 参入4条件・出口・トレールすべて確定足クローズで判定 |
| A-3 SL単調性 | ✅ `_advance_sl_to` が利益方向のみ許可(hypothesis で担保) |
| A-4 防御トリガー種別=手法契約 | ✅ 含み益トリガー(TP1建値化+トレール)を自手法契約として採用。A-3 は順守 |
| A-5 float禁止 | ✅ 価格は `*_int`、EMA/ATR/SMA は量子化 Decimal |
| A-6 UTC固定 / A-7 スキーマ固定 | ✅ Candle / `AtrTrendPlan`(frozen) |
| A-8 Enum一本FSM | ✅ `AtrState(IDLE/OPEN/RUNNER/CLOSED)` |
| A-9 冪等性 / A-10 イベントソーシング / A-11 純粋関数 | ✅ 既存 loop/journal 経路 |
| A-12〜15 AI層 | △ 参入ゲートはパススルーで無効化(§0)。防御層は別途全手法強制 |
| **B(Narrow Focus 固有)** | **非適用**(コンフルエンス/半利/エリオット/建値ダウFSM は narrow_focus 専用) |

---

## 5. プラットフォーム統合

- **L2 分析層** `provider.py`: `AtrTrendScalpProvider`(`SignalProvider` を構造的に充足)。
  EMA9/21・ATR14・出来高SMA20・平均ATR20(deque)・M15リサンプル→EMA50・押し目リング窓・
  単一ポジション・ミラー(`reset_position_mirror()` ウォームアップ・フック付き。smc_bos と同契約)。
- **L2 執行層** `execution.py`: `AtrTrendExecution`(`core.execution.ExecutionModel` を充足)。
  `market_tpsl`/`smc_bos` の成行執行を出発点に RUNNER 状態を1段追加。
- **L2 上位足** `resample.py`: `TfResampler`(M5→M15。`MacroResampler` と同型を自前実装。
  L2→L2 依存回避。将来 L1 昇格の余地)。
- **L2 語彙** `signals.py`: `AtrTrendPlan`(frozen。分割決済ジオメトリ tp1_int/
  trail_distance_ticks を明示。report_html 互換のため TradePlan 相当の中立フィールドも保持)/
  `AtrTrendOutput`。
- **レジストリ** `registry.py`: `StrategySpec(name="atr_trend_scalp", build, build_execution,
  report_spec)` を1件登録(既存登録は不変)。
- **必要インジケーター(L1)**: EMA/ATR/SMA すべて既存。**新設不要**。
- **`Timeframe`**: M5/M15 とも既存。**enum追加不要**(smc_bos の M30 追加より軽い)。

### CLI
```powershell
# データ取得(M5・大容量。まず短期で検証)
python -m infers.main --mode export --data data/xauusd_m5.parquet --tf M5 --symbol XAUUSD --years 2
# バックテスト(パススルーゲート)
python -m infers.main --mode backtest --data data/xauusd_m5.parquet --strategy atr_trend_scalp --tf M5 --symbol XAUUSD --ai-client none --report reports/atr_trend_scalp_full
# ライブ(別口座 = 別プロセス・別MT5ターミナル・別 --journal。デモ必須)
python -m infers.main --mode live --demo --strategy atr_trend_scalp --tf M5 --symbol XAUUSD --ai-client none --journal work/journal/atr_xau.jsonl
```

---

## 7. パラメータ一覧(既定値)

| パラメータ | 既定 | 説明 |
|---|---|---|
| `tf` / `htf` | M5 / M15 | 執行TF / 上位足バイアスTF |
| `ema_fast_period` / `ema_medium_period` | 9 / 21 | M5 モメンタム(ゴールデン/デッドクロス) |
| `ema_trend_period` | 50 | M15 上位足トレンドフィルタ |
| `atr_period` | 14 | ATR(SL/TP/トレール/ボラフィルタの基準) |
| `vol_sma_period` / `atr_avg_period` | 20 / 20 | 出来高SMA / 平均ATR |
| `retrace_lookback` | 3 | 押し目タッチの有効窓(本) |
| `atr_vol_mult` / `vol_mult` | 1.1 / 1.2 | ボラ拡大 / 出来高増の必要倍率 |
| `sl_atr_mult` | 1.0 | 初期SL距離 = ×ATR |
| `tp1_atr_mult` / `tp2_atr_mult` | 1.0 / 2.0 | TP1 / TP2 距離 = ×ATR |
| `trail_atr_mult` | 0.5 | トレール幅 = ×ATR(エントリー時ATR固定) |
| `min_stop_distance_ticks` | 5 | SL下限(LedgerBroker 下限を満たす) |
| `volume_steps` | 2 | 固定ロット(50/50分割のため偶数推奨。`--risk-pct` で上書き) |
| `session_filter` | **False** | JST16:00〜25:00(UTC07-16)ゲート(opt-in) |
| `max_spread`(RiskManager) | 既存 | スプレッド異常拒否(金はスプレッド影響大) |

---

## 8. データ要件

- XAUUSD M5 確定足 Parquet(列: time[UTC]/open/high/low/close/**volume**)。volume は必須
  (条件4のダマシ排除)。export 後に volume が 0/欠損でないことを確認する。
- `--from/--to/--last` で期間スライス(phase2 §5)。レポートは `reports/atr_trend_scalp_full/`。

---

## 9. 検証(完了ゲート)

1. **depth50 ビット完全一致**(`--strategy depth50`: 624トレード / PF1.489 / DD$326 / 勝率53.37%)
   が1ビット不変。
2. **`pytest` 全合格**(新規 `tests/test_atr_trend_scalp.py` 26件を含む)。状態機械・SL単調性は
   hypothesis プロパティテストで担保。
3. **CLI 完走**: `--strategy atr_trend_scalp --tf M5 --ai-client none` が XAUUSD M5 で完走し
   トレード発生。
4. **再現バックテスト**: `reports/atr_trend_scalp_full/` 生成 + `health_check.md` に所見記録
   (本手法に出典実績値は無く、自前実測が唯一の真実)。

---

## 10. 段階的実装計画

| 段階 | 内容 | 状態 |
|---|---|---|
| **A0** | `signals.py` / `resample.py` / `__init__.py` + 単体テスト | ✅ 完了(2026-07-01) |
| **A1** | `provider.py`(4条件ゲート・ATRジオメトリ・ミラー) + 単体テスト | ✅ 完了 |
| **A2** | `execution.py`(FSM・分割決済・建値化・トレール・SL単調性プロパティ) + テスト | ✅ 完了 |
| **A3** | `registry.py` 登録 + `report.py` + 結合(パススルーゲート)テスト | ✅ 完了 |
| **A4** | XAUUSD M5 データ export → 全期間バックテスト → `reports/atr_trend_scalp_full/`。出来高フィルタ実効性・含み益トリガーの損益寄与を分析 | ⏳ 未(データ依存) |
| **A5(opt-in)** | `session_filter` の ON/OFF 実測比較 | ⏳ 未 |

---

## 11. 原典(仕様書)との差異(明示)

1. **エントリー約定**: 形成中バーでなく**確定足終値**でのシグナル判定(§A-2)。
2. **数値表現**: 価格は整数ティック、EMA/ATR/SMA は量子化 Decimal(float禁止 §A-5)。
3. **出来高**: XAUUSD CFD は真の出来高が無く **tick_volume** を代替に用いる。
4. **上位足**: M15 は独立フィードでなく M5 からの内部リサンプル(単一の真実=最小足)。
5. **AIゲート**: 参入は**パススルー**(意味的に不要。防御層は別途強制)。
6. **複利/実口座**: 本書スコープ外(デモ検証必須・自己責任)。

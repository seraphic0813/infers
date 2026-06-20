# M30 SMC BOS + EMA80 手法 仕様書(`smc_bos`)

> **ステータス: 導入完了(S0〜S4・§11論点A〜F全解決。S5は見送り)** — 2026-06-20 起案・同日完了。
> 本書は INFERS プラットフォームに3つ目の手法 `smc_bos` を組み込むための設計文書。
> M30+EMA80の前提整備(S0)・パススルー・ゲート(S1)・手法本体(S2)・M30実データでの初回検証(S3)・
> SL前進実装+実測比較(S4)が完了。`be_mode` 既定は実測データに基づき `off` で確定(§3.3)。
> `rr_target=3.0` も感度分析の結果変更なしで確定(§11論点B。過剰適合のため)。
> S5(任意拡張: HTFバイアス/CHoCH/トレーリング/ニュースフィルタ)はユーザー判断により見送り、
> ここを smc_bos 導入の完了点とする。進捗は §10 参照。
>
> **本書の位置づけ(文書優先順位)**:
> - プラットフォーム全体の正は [phase2-architecture.md](../../../../docs/phase2-architecture.md)(6層化・手法/執行抽象)。
> - 本手法の**エントリー判定ロジックの正は本書**(`strategies/smc_bos/` 配下に同居。CLAUDE.md の配置方針に従う)。
> - 本書は Narrow Focus / depth50 / market_tpsl の挙動を**1ビットも変えない**ことを前提とする。

---

## 0. 要約(TL;DR)

- **何を足すか**: XAUUSD M30 の Smart Money Concepts。**BOS(Break of Structure)** を中心に **EMA80** でトレンド方向を確認し、構造ブレイクの瞬間に成行参入する手法。Narrow Focus(多段コンフルエンス+指値打診+半利+ランナー)とも market_tpsl(SMAクロス+固定TP/SL)とも別の、3つ目の独立手法。
- **なぜ足すか**: 出典(ForexFloor の4年2ヶ月バックテスト)で PF 2.55・Sharpe 4.58 と報告される実績派。低勝率(44.5%)を高RRで補う構造ベース戦略で、パラメータが少なくオーバーフィットしにくいとされる。プラットフォームが「執行ライフサイクルの異なる手法を載せ替えられる」ことの2例目の実証にもなる。
- **実装の核**: `strategies/smc_bos/`(`provider.py` = BOS+EMA80 シグナル / `execution.py` = 成行+固定RR利確の `SmcExecution`(SL前進`be_mode`はopt-in、既定`off`。実測で最良だった構成))+ レジストリ1行登録。
- **着手前に解消が必要な4つの前提(本書 §5.6 / §5.3 / §5.7 / §3.3 で詳述)**:
  1. **M30 が `Timeframe` enum に無い** → L0 `core/models.py` に追加(加算的・安全)。
  2. **EMA インジケーターが無い**(L1 は SMA/ATR/RSI のみ)→ `indicators/ema.py` を新設。EMA は SMA と別物。
  3. **AIゲートが Narrow Focus 特徴量に結合**しており、SMC のプランは現状すべて GUARDRAIL NO_GO になる(market_tpsl と同じ既知問題)→ 手法非依存の**パススルー・ゲート**を用意する。
  4. ~~「1:1到達でBE」が CLAUDE.md §A-4(含み益トリガー禁止)に抵触~~ → **解消済み(2026-06-20)**: §A-4 を**手法スコープ化**する CLAUDE.md 改訂を実施。含み益トリガー禁止はもはやグローバル不変条件ではなく**各手法の契約**となったため、SMC は原典どおり「**1R 到達で建値化(含み益トリガー)**」を**自手法の契約として採用してよい**。全手法に残る防御の不変条件は **A-3(SL単調性=利益方向にしか動かさない)** のみで、1R 建値化はこれを満たす。詳細 §3.3。

---

## 1. 出典と実績(ForexFloor 4年検証)

| 項目 | 値 |
|---|---|
| 検証元 | ForexFloor ブログ「How we backtested Gold algorithm」(https://www.forexfloor.id/blog/how-we-backtested-gold-algorithm/) |
| 対象 | XAUUSD(金) |
| 時間軸 | M30(30分足) |
| 期間 | 2022年1月〜2026年3月(4年2ヶ月。2022年の金下落相場を含む) |
| トレード数 | 391回 |
| PF | 2.55 |
| 勝率 | 44.5%(低勝率を高RRで補う) |
| Sharpe | 4.58 |
| Recovery Factor | 3.98 |
| Max DD | 37.54%(リスク3%時) |
| 複利例 | $5,000 → $4.2M超(84,277%、リスク管理前提) |

> **免責**: 上記はすべて**外部ブログの自己申告値**であり、当方で再現・検証していない。DD 37% は実運用では極めて重く、**本数値は実装後に自前バックテスト(`reports/smc_bos_full/`)で再検証するまで「目標」ではなく「出典の主張」として扱う**(本プロジェクトの depth50 が出典PF3.92→実測1.07だった前例がある)。複利・実口座運用は本書のスコープ外(デモ検証必須)。

> **段階S3 初回実測(2026-06-20、`reports/smc_bos_full/`)**: XAUUSD M30・5年(2021-06〜2026-06・
> 59,142本、MT5 Vantage Trading Demoから取得)・段階S2構成(`be_mode=off`・既定パラメータ・
> `--ai-client none`)で実測した結果、**トレード数197・PF 1.456322103・勝率31.98%・最大DD$911.78・
> 純益+$4,258.48(5年/固定2step)**。出典主張(PF2.55・勝率44.5%・391トレード)を**下回るが
> depth50ほど劇的な乖離(3.92→1.07)ではない**。乖離要因はスイング検出方式の違い・単一ポジション
> 制約の実装(自己ミラー追跡)・`be_mode=off`(出典の1R建値化が未実装)等と推測(未検証の仮説)。
> 詳細は `reports/README.md` 「smc_bos (3つ目の手法・別TF)」節。

---

## 2. シグナルロジック(エントリー判定の正)

設計思想: **「機関投資家の注文フローが市場構造を変えた瞬間(BOS)を、中期トレンド(EMA80)に順行する側でのみ取る」**。Narrow Focus のような多段コンフルエンスは課さない(SMC は構造ブレイク+1本のEMAという少パラメータが身上)。判断はすべて **M30 確定足クローズ時のみ**(CLAUDE.md §A-2 確定足主義)。

### 2.1 時間軸

- 判定TF = **M30**(レジストリ既定。`--tf M30` で駆動)。ノイズと有意トレード数のバランスが良いとされるTF。
- 補助(任意・段階2): HTF(H1/H4)バイアス確認。

### 2.2 BOS(Break of Structure)= 中核

直近の確定スイング高値/安値を、**確定足の終値**が明確にブレイクした事象。

- **強気BOS(bull)**: M30 確定足終値 > 直近の確定スイング高値 + バッファ。
- **弱気BOS(bear)**: M30 確定足終値 < 直近の確定スイング安値 − バッファ。
- **スイングの定義**: フラクタル/ピボット型(ある高値の左右 `swing_lookback` 本がすべてそれより低い → スイング高値。安値は対称)。出典EA の `FindSwings`(単純な N 本前の high/low)は**ダマシに弱いため採用せず**、確定遅延つきのピボット検出を用いる(リペイント防止。§5.4)。
- **バッファ**: `breakout_buffer = max(α_atr × ATR, n_ticks)`(ヒゲの瞬間抜け・スプレッド分のダマシを除外。CLAUDE.md の「第1波高値超え」と同型)。
- **確定足主義**: ブレイク判定は終値のみ。形成中バーのヒゲ抜けでは発火しない(§A-2)。

### 2.3 CHoCH(Change of Character)= 補助(段階2、既定OFF)

トレンドに対する**最初の逆方向構造ブレイク**(反転の初動)。段階1では実装せず、BOS 一本に集中する。段階2で「直近トレンド方向の保持」フィルタ、または反転エントリーの種として導入を検討。

### 2.4 EMA80 フィルタ = 最重要フィルタ

中期トレンドの方向確認。**EMA(指数移動平均)であって SMA ではない**(出典が EMA80 を明示。L1 に EMA が無いため新設が必要 — §5.3)。

- **ロング許可条件**: 確定足終値 > EMA80。
- **ショート許可条件**: 確定足終値 < EMA80。
- 期間 80 は出典が 50〜200 のテストで最適とした値。`ema_period` でパラメータ化(既定80)。

### 2.5 エントリー条件(統合)

```
ロング:   強気BOS成立 AND 終値 > EMA80(M30)   → 成行ロング
ショート: 弱気BOS成立 AND 終値 < EMA80(M30)   → 成行ショート
```

- BOS とEMAフィルタの両方が同一確定足で成立したときのみ、成行参入プランを **1件** 出す。
- **同時に1ポジションのみ**(`NoOpenPosition` 相当。出典の「1トレード制限」)。建玉中は新規プランを出さない(冪等性は loop の `plan_id ∈ open_positions` でも担保)。
- 参入参考価格 = 確定足終値(成行のため実約定はブローカー次第。SimBroker は確定足クローズ価格で約定)。

---

## 3. 防御・執行(出口)

CLAUDE.md §A の安全原則は**全手法に無条件で強制**される。SMC も例外ではない:
損切り・SL移動・利確・強制手仕舞いは **100%決定論の Python**(LLM非依存、§A-1)。

### 3.1 初期SL(発注と同時に必ず設定)

- **第一候補(構造SL・SMC本流)**: セットアップの直近スイング(ロングなら直近スイング安値、ショートなら直近スイング高値)の**外側** `sl_buffer` ティック。
- **下限ガード**: 構造SLが近すぎる場合は `max(構造距離, atr_sl_mult × ATR, min_stop_distance)` で下限を設ける(LedgerBroker の `min_stop_distance_ticks=5` 以上を必ず満たす。出典の `ATR_Mult_SL=1.5` をATR下限として流用)。
- SLなしでポジションを保持する状態は作らない(成行約定と同一注文でSL設定。market_tpsl と同方針)。

### 3.2 TP / RR

- **固定RR利確**: TP = エントリー + `rr_target × |エントリー − 初期SL|`(方向符号つき)。
- `rr_target` 既定は **3.0**(出典は箇所により「5:1」「2.5〜3.0」「1:2〜1:3」と揺れがあるため、再現検証で最適化する前提でパラメータ化。§11-論点B)。
- 確定足の高値(ロング)/安値(ショート)が TP へ到達したら全決済(確定足主義。market_tpsl と同じ判定)。
- 段階2: トレーリング利確モード(`exit_mode=trailing`)を選択肢として用意(出典は「5:1 またはトレーリング」)。

### 3.3 SL前進(建値化 / トレーリング)— SMC 固有の防御契約

> ✅ **2026-06-20 更新**: かつて本節は「出典の『1:1 BE』が CLAUDE.md §A-4(全手法共通・絶対)の含み益トリガー禁止に抵触するため、構造イベント駆動へ翻訳する」としていた。その後 **§A-4 を手法スコープ化**する CLAUDE.md 改訂を実施し、防御調整のトリガー種別は各手法の契約になった。**全手法に残る不変条件は A-3(SL単調性=利益方向にのみ移動)のみ**。したがって SMC は**原典どおりの含み益トリガー BE を自手法の契約として採用できる**。

本手法は以下の SL前進モードを持つ(`be_mode` で選択):

- **`be_mode=off`(既定。段階S4で確定)**: SL前進なし。固定SL+固定TPのみ(market_tpsl 同等)。
- **`be_mode=at_1r`(原典準拠・opt-in)**: 含み益が初期リスク幅(1R = `|エントリー − 初期SL|`)に達した確定足で、SL を**建値(実約定価格)**へ移動する。出典「1:1到達でBE」をそのまま実装する SMC の手法固有契約。
  - **A-3(SL単調性)順守**: 建値は初期SLより必ず利益方向にあるため、移動は常に利益方向(`_advance_sl_to` が改善のみ許可)。
  - **トリガー = 含み益(1R)**: これは §A-4 改訂により SMC の契約として明示的に許可された(CLAUDE.md A-4。Narrow Focus はこの自由を使わない)。判定は含み益額/pips ではなく**確定足の高値(ロング)/安値(ショート)が 1R 価格水準へ到達したか**で行う(確定足主義 §A-2。実装上は価格比較であり PnL 評価ではない)。
- **`be_mode=structure`(代替・構造ベース・opt-in)**: トレード方向に新しい確定スイングが形成されたら SL をその保護スイングへ前進(Narrow Focus と同型の構造駆動)。含み益トリガーを避けたい場合の選択肢。A-3 順守。

> **段階S4 実測結果(2026-06-20、XAUUSD M30・5年・スワップ込み)**: 3モードを同一データで比較した結果、**`off` が PF・純益のいずれでも `at_1r`/`structure` を一貫して上回った**。`at_1r` は出典準拠だが全指標で `off` に劣後(支配される関係)。`structure` は勝率・DDは改善するが PF が1.04まで低下し5年純益がほぼゼロ。

| be_mode | トレード数 | PF | 勝率 | be_sl_exit_rate | 最大DD | 純益(5年) |
|---|---|---|---|---|---|---|
| **off**(既定) | 197 | **1.456322103** | 31.98% | 0% | $911.78 | **+$4,258.48** |
| at_1r | 197 | 1.383701289 | 20.81% | 5.08% | $1,027.92 | +$2,339.68 |
| structure | 197 | 1.042033434 | 41.12% | 36.55% | $843.42 | +$144.88 |

**読み方**: `at_1r`(早期建値化)は「勝ちトレードを伸ばす前に引き上げる」副作用で win_rate・PF・純益のすべてを悪化させた。これは Narrow Focus の entry-methodology.md が含み益トリガーのSL移動を**そもそも禁じている理由**(「含み益が出た瞬間に建値SLにするとノイズに狩られる」)と同質の現象が SMC(別ロジックの手法・別TF)でも再現されたことを示す。`structure` は対称的に「保護が遅すぎて伸ばせる場面でも稀に深く戻される」側に振れた可能性があるが未検証(仮説)。

**判定: `be_mode=off` を実装上の既定とする。`at_1r`/`structure` は出典準拠・代替アプローチとして実装・テストは維持するが opt-in。** 詳細は `reports/README.md`「smc_bos」節 実験(2026-06-20)。

### 3.4 執行ライフサイクル(`SmcExecution` 状態機械)

`ExecutionModel` プロトコル(§5.2)を充足する有限状態機械(Enum 一本。§A-8)。market_tpsl の `IDLE→OPEN→CLOSED` を拡張し、構造ベースSL前進を1段挟む:

```
IDLE ──place()──▶ OPEN ──(1R到達 or 新スイング確定: be_mode≠off)──▶ OPEN(SL前進済み)
                   │                                              │
                   ├──on_bar: TP到達 / on_broker_event: SLヒット──┴──▶ CLOSED
                   └──close(): データ末尾・シャットダウン ─────────────▶ CLOSED
```

- `place(intent)`: 成行参入 + 初期SL設定(冪等 `client_order_id`)。`intent` は TradePlan を duck-typing で受ける(market_tpsl と同様)。
- `on_bar(candle, signal)`: 確定足ごとに ①TP到達判定(全決済)②SL前進(`be_mode=at_1r`: 確定足が 1R 価格水準へ到達したら建値へ / `be_mode=structure`: `signal` の新スイング情報へ前進 / `off`: 何もしない)。前進は必ず利益方向(A-3)。`BarOutcome(closed=..., expired=False)` を返す(SMC は指値失効が無いため `expired` は常に False)。
- `on_broker_event(ev)`: SLヒットで CLOSED。
- `close(reason)`: 残玉を成行手仕舞い。
- 半利・ランナー・ADD は**持たない**(SMC は1ポジション/単一TP。§B のコンフルエンス/半利契約は Narrow Focus 専用で SMC には非適用)。

---

## 4. リスク管理

- **サイジング**: 固定リスク%(出典推奨 1〜2%、最大3%)。プラットフォーム既存の `VolumeSizer` + `BacktestEquityProvider`(`--risk-pct` 指定時)をそのまま使う。loop は `sl_dist = |limit − sl|` からステップ数を算出するため、成行参入でも機能する。複利($5k→…)は `BacktestEquityProvider` の equity 連動で表現可能。
- **DD**: 出典 Max DD 37.54%(リスク3%)。**実運用は DD を耐えられる資金前提**。本プロジェクトでは `RiskManager`(日次損失上限・キルスイッチ・建玉上限)が全手法に強制されるため、これらと併用する。
- **禁止事項(出典・本プロジェクト共通)**: マーチンゲール / グリッド / ナンピン(ポジションアベレージング)なし。`SmcExecution` は構造上これらを表現できない(1ポジション・SL単調・ADD無し)。

---

## 5. プラットフォーム統合(層・契約マッピング)

### 5.1 分析層 `provider.py`(L2)

- クラス `SmcBosProvider`(`SignalProvider` を構造的に充足: `on_candle(candle) -> ProviderOutput`)。
- 内部に EMA80(L1)・ATR(L1)・スイング/BOS検出器(§5.4)を保持。
- 出力は `SmcOutput`(`strategies/smc_bos/signals.py`。段階S4で確定)。`plans: list[TradePlan]` は narrow_focus/signals.py の共通語彙を market_tpsl と同じく流用し、Narrow Focus 固有フィールドは中立値で埋める:
  - 使うフィールド: `direction` / `limit_price_int`(=終値)/ `volume_steps` / `sl_int`(初期SL)/ `fib_target_int`(=固定TP として流用)/ `expiry`(=遠未来)/ `invalidation_price`(=初期SL)。
  - 未使用: `w1_high_int=0` / `add_volume_steps=0` / `cluster_score`・`ambiguity` は中立値(ゲート tier 判定を通すためのプレースホルダ)。
  - `SmcOutput.swing_high_int`/`swing_low_int` は `be_mode=structure` のSL前進に必要な「新スイング情報」(毎確定足の最新値。`narrow_focus.signals.ProviderOutput` は再利用しない — Narrow Focus 固有語彙[`structure_events`等]を持つため。L0は `output.plans` のみをduck-typingで読み `signal` は不透明に扱うので型の流用は不要)。
- `request`: `JudgementRequest(kind=ENTRY_GATE, features={"strategy":"smc_bos", ...})`。ただし下記 §5.7 のとおり、この request は SMC では実質パススルー対象。

### 5.2 執行層 `execution.py`(L2)

- `SmcExecution`(§3.4)。`infers.core.execution` の `ExecutionModel` / `BrokerPort` / `BarOutcome` のみに依存(L2→L0 の正方向依存)。market_tpsl の `execution.py` を出発点に、構造ベースSL前進を `on_bar` に1段追加した形。
- 安全原則の充足:
  - §A-1 LLM非依存 — 本モジュールは LLM を一切呼ばない。
  - §A-2 確定足主義 — `on_bar` は `candle.is_closed` を要求。
  - §A-3 SL単調性 — SL前進は利益方向のみ。`modify_sl` は `SLMonotonicGuard` 経由(逆行は例外)。
  - §A-4 含み益トリガー禁止 — SL前進トリガーは**構造イベントのみ**(PnL/pips を見ない)。
  - §A-9 冪等性 — 全注文に決定論的 `client_order_id`。

### 5.3 必要インジケーター(L1)

| 指標 | 状態 | 対応 |
|---|---|---|
| **EMA(指数移動平均)** | **❌ 未実装**(L1 は SMA/ATR/RSI のみ) | **`indicators/ema.py` を新設**。整数ティック入力・固定量子化 Decimal 出力(`Q`)で `SMA` と同じ決定論規約。漸化式 `EMA_t = EMA_{t-1} + k(price − EMA_{t-1})`, `k = 2/(period+1)`。係数 `k` は Decimal 固定。初期値は最初の `period` 本の SMA でシード(プラットフォーム非依存の決定性を担保)。`__init__.py` で再公開 |
| ATR | ✅ 実装済み(`indicators/atr.py`) | そのまま使用(バッファ・SL下限・ボラフィルタ) |
| スイング/BOS検出 | △(ZigZag が L2 narrow_focus に存在) | §5.4 |

> **注意**: EMA の決定論性(バックテスト⇄ライブ同一)は L1 の絶対要件(`indicators/__init__.py` 冒頭)。浮動小数を使わず毎ステップ量子化する。係数 `k` の桁・丸めをテストで固定する。

### 5.4 構造検出(スイング/BOS)の置き場所

- **段階1: 手法フォルダ内に自前実装**(`strategies/smc_bos/structure.py`)。フラクタル/ピボット型スイング検出 + BOS/CHoCH 判定。確定遅延つき・frozen・リペイント禁止(§A-2)。
  - 既存の `strategies/narrow_focus/zigzag.py`(`ZigZagDetector`)は閾値反転型でBOSの参照スイングにそのまま流用しづらく、かつ**他手法フォルダへの L2→L2 依存は避ける**(CLAUDE.md「手法固有計算は手法フォルダ内」)。よってまず自前実装する。
- **段階3候補: L1 への昇格**。スイング検出が SMC でも使われ「複数手法で再利用する汎用部品」になったら、`indicators/` への吸い上げを検討(CLAUDE.md の定石4・5)。ただし昇格は narrow_focus のビット完全一致を脅かさない形に限る(depth50 は `ZigZagDetector` 依存のため、移動ではなく新規汎用版の追加が無難)。

### 5.5 レジストリ登録(`strategies/registry.py`)

`StrategySpec` を1件追加(market_tpsl と同じ二点登録パターン):

```python
def _build_smc_bos(*, symbol, tf):
    from infers.strategies.smc_bos.provider import SmcBosProvider
    return SmcBosProvider(symbol=symbol, tf=tf)   # 既定パラメータ

def _build_smc_execution(*, position_id, direction, broker, config, journal_sink=None):
    from infers.strategies.smc_bos.execution import SmcExecution
    return SmcExecution(position_id=position_id, direction=direction,
                        broker=broker, config=config, journal_sink=journal_sink)

register(StrategySpec(
    name="smc_bos",
    build=_build_smc_bos,
    build_execution=_build_smc_execution,
    description="M30 SMC BOS + EMA80 フィルタ (XAUUSD)。構造ブレイク成行参入 + "
                "構造ベースSL前進 + 固定RR利確。Narrow Focus / market_tpsl とは別の執行ライフサイクル",
))
```

- CLI: `python -m infers.main --mode backtest --data <XAUUSD_M30.parquet> --strategy smc_bos --tf M30 --symbol XAUUSD ...`。
- `--strategy` 経路は `build_execution` を `execution_factory` として配線済み(main.py `build_execution_factory`)。**depth50 経路(execution_factory=None)は不変**。

### 5.6 `Timeframe` enum への M30 追加(**必須前提**)

- `core/models.py` の `Timeframe` enum は現在 `M5/M15/H1/H4/D1/W1` のみで **M30 が無い**。
- 対応: enum に `M30 = "M30"` を、`_DURATIONS` に `Timeframe.M30: timedelta(minutes=30)` を追加(**加算的・後方互換**)。これは L0 への変更だが、既存値に触れないため depth50 のビット一致を脅かさない。
- データ export(`--mode export --tf M30`)・feed・確定足判定が M30 を受けられることを確認する(`tf.duration` 経由のため自動対応の見込み。テストで確認)。

### 5.7 AIゲート結合の扱い(**S1 で解消済み・2026-06-20**)

- **問題**: `TradingLoop.on_candle` は全プランに `gateway.judge(plan.request, ...)` を強制する。既定の `RuleBasedLlmClient` は **Narrow Focus 固有特徴量**(`dow_state`/`w1_high`/`rsi_band`/`families`…)を `judge_features` で参照するため、SMC の features では `KeyError` → `_resolve` の例外捕捉で **GUARDRAIL NO_GO**。結果、SMC は実装前は**1トレードも約定できない**(market_tpsl で実証済みの既知制約。phase2 §段階2.5)。
- **SMC は本来 LLM/ルールゲートを必要としない**: エントリー判定(BOS確定 + EMA80)は provider 内で完結する決定論ゲートそのもの。LLMゲートは Narrow Focus のコンフルエンス審査用であり、SMC には意味的に不要。防御層(SL単調・冪等・リスク拒否権)は別途すべての手法に強制され続ける(これは「約定可否」の問題であって安全性の問題ではない。phase2 §段階2.5 と同じ整理)。
- **対応(案A・実装済み)**: `--ai-client none` を新設し、常に GO を返す `infers.ai.passthrough.PassthroughLlmClient` + 全件 `Tier.L1_ONLY` に落とす `PASSTHROUGH_POLICY` を `main._build_gateway` に配線(`tests/test_passthrough.py`)。**どの手法でも**ゲートを意味的に無効化でき、SMC/market_tpsl の素の執行をバックテストできる。防御層は不変。CLI実証: `--strategy market_tpsl --ai-client none --last 3m` で414トレード・全件 `L1:GO`(GUARDRAIL無し)を確認。
  - 案B(ゲートを手法-aware に一般化。provider がゲートの要否・特徴量を宣言)は phase2 が「フェーズ3課題」とした本筋だが大改修のため不採用(本手法導入のスコープ外)。
  - 案C(SMC features を無理に Narrow Focus 形へ詰めて既存ルールゲートを通す)は意味的に破綻するため不採用。
- S2 の結合検証は `--ai-client none` 経由(`AiGateway` 実体を使う。market_tpsl の `_GoGateway` 手書きスタブより高忠実度)で `SmcExecution` がエントリー→TP/SL決済まで成立することを確認する。

---

## 6. 守るべき不変条件と SMC 固有の扱い

| 区分 | 条項 | SMC での扱い |
|---|---|---|
| **A(全手法共通・絶対)** | A-1 防御はLLM非依存 | ✅ execution は LLM 非依存 |
| | A-2 確定足主義 | ✅ BOS/EMA/出口すべて確定足クローズで判定 |
| | A-3 SL単調性 | ✅ SL前進は利益方向のみ。`_advance_sl_to` が改善のみ許可(プロパティテスト済み) |
| | A-4 防御トリガー種別は手法契約 | ✅ §A-4 改訂で手法スコープ化。SMC は**含み益(1R)トリガーBEを自手法の契約として実装可能**(`be_mode=at_1r`、opt-in)だが、実測(§3.3)で `off`(既定)に劣後したため既定では使わない。A-3 のみ全手法強制で、それは順守 |
| | A-5 float禁止 | ✅ 価格は `*_int`、EMA/ATR は量子化 Decimal |
| | A-6 UTC固定 | ✅ Candle が強制 |
| | A-7 スキーマ固定 | ✅ `SmcOutput`/`TradePlan`/frozen dataclass |
| | A-8 Enum一本のFSM | ✅ `SmcState`(IDLE/OPEN/CLOSED) |
| | A-9 冪等性 | ✅ 決定論 `client_order_id` |
| | A-10 イベントソーシング | ✅ 判断を journal へ記録(loop/execution 既存経路) |
| | A-11 戦略コアは純粋関数 | ✅ I/O はアダプタ隔離。TradingLoop は `ExecutionModel` 抽象のみ呼ぶ |
| | A-12〜15 AI層規約 | △ SMC はゲートをパススルーで無効化(§5.7)。LLM を使う場合も事前計算特徴量のみ |
| **B(Narrow Focus 固有)** | コンフルエンス必須・半利・エリオット・建値ダウFSM 等 | **非適用**(B は narrow_focus/depth50 専用。SMC は単一根拠 BOS+EMA で可) |

---

## 7. パラメータ一覧(既定値案)

| パラメータ | 既定 | 説明 |
|---|---|---|
| `tf` | M30 | 判定TF |
| `symbol` | XAUUSD | 対象銘柄 |
| `ema_period` | 80 | EMAフィルタ期間(出典最適値) |
| `swing_lookback` | 5 | ピボットスイングの左右本数(出典 5〜10) |
| `breakout_buffer_atr` | 0.3 | BOSバッファ = max(係数×ATR, n_ticks) |
| `breakout_buffer_ticks` | 数ティック | 同上の下限 |
| `atr_period` | 14 | ATR 期間 |
| `sl_mode` | structure | 初期SL = 構造(直近スイング外側)。代替: `atr` |
| `sl_buffer_ticks` | 数ティック | 構造SLの外側余白 |
| `atr_sl_mult` | 1.5 | SL下限の ATR 倍率(出典 ATR_Mult_SL) |
| `rr_target` | 3.0 | 固定RR(出典は 2.5〜3.0 / 5:1 と揺れ。要最適化) |
| `exit_mode` | fixed_rr | 固定RR利確。代替: `trailing`(段階2) |
| `be_mode` | **off**(段階S4実測で確定) | SL前進トリガー。`off`=前進なし(既定) / `at_1r`=含み益1Rで建値化(原典準拠・opt-in) / `structure`=新スイングで前進(opt-in)。実測比較は §3.3 |
| `volume_steps` | 2 | 固定ロット時の建玉(リスク%指定時は VolumeSizer が上書き) |
| `max_spread_ticks` | (RiskManager 既定) | スプレッド異常拒否(金はスプレッド影響大) |

> 段階S2は `be_mode=off`・`exit_mode=fixed_rr`・`sl_mode=structure` の最小構成から開始し、段階S4で `at_1r`/`structure` を実装・実測した結果、`off` を既定として確定した(§3.3)。

---

## 8. データ要件

- **必要データ**: XAUUSD M30 の確定足 Parquet(出典期間 2022-01〜2026-03 を含む長期)。
- 取得: `python -m infers.main --mode export --data <out_M30.parquet> --tf M30 --symbol XAUUSD --years 5`(§5.6 の M30 追加が前提)。
- データは「1ファイルを正とし期間スライス」(phase2 §5)。`--from/--to/--last` で期間を切り出す。レポート出力は `reports/smc_bos_full/`・`reports/smc_bos_<期間>/`。

---

## 9. 検証計画(完了ゲート)

各段階の**完了ゲート = 下記すべてを満たす**:

1. **depth50 ビット完全一致**: 既存ベースライン(`--macro-wave2 --depth-screen --depth-max 0.50 --no-fib-score`)が PF 1.487396352 / 624トレード / DD$325.77 / 勝率53.37% と1ビットも変わらない(`execution_factory=None` 経路・rule gate 経路ともに不変)。
2. **`pytest` 全合格**(現行331件 + 新規)。状態機械・注文ロジックの変更はプロパティテスト(hypothesis)追加必須(§A-8/A-3 の不変条件)。
3. **新規テスト(想定)**:
   - L1 EMA: 既知系列での値・量子化・ウォームアップ・決定論性。
   - 構造検出: スイング確定遅延・BOS/CHoCH 判定・リペイント無し。
   - `SmcExecution`(単体): place→TP決済 / place→SLヒット / 構造SL前進が利益方向のみ(SL単調性プロパティ) / close。
   - レジストリ配線: `--strategy smc_bos` で provider+execution が組み上がる。
   - エンジン結合: 寛容ゲート注入で BacktestEngine→TradingLoop 経由のエントリー→決済が成立。
   - M30: `Timeframe.M30` の duration・確定足判定・スライス。
4. **CLI 完走**: `--strategy smc_bos --tf M30`(案A パススルーゲート併用)が XAUUSD M30 データで完走し、トレードが発生する。
5. **再現バックテスト**: `reports/smc_bos_full/` を生成し、出典(PF2.55)との乖離を `health_check.md` に記録(depth50 同様、出典値の盲信はしない)。

---

## 10. 段階的実装計画(案)

| 段階 | 内容 | リスク | 状態 |
|---|---|---|---|
| **S0** | 前提整備: `Timeframe.M30` 追加(L0)+ `indicators/ema.py` 新設(L1)+ 単体テスト。depth50 ビット一致確認 | 低(加算的) | **完了**(2026-06-20。[PR #1](https://github.com/seraphic0813/infers/pull/1)) |
| **S1** | パススルー・ゲート(案A: `--ai-client none`)を CLI/gateway に追加。手法非依存・防御層不変 | 低〜中 | **完了**(2026-06-20。`infers/ai/passthrough.py` + `test_passthrough.py`。depth50 ビット一致再確認済み) |
| **S2** | `strategies/smc_bos/` 実装(`structure.py`/`provider.py`/`execution.py`)。`be_mode=off`・`fixed_rr` の最小構成。レジストリ登録。単体+結合テスト | 中 | **完了**(2026-06-20。`SwingDetector`/`bos_direction`/`SmcBosProvider`/`SmcExecution`。単体30件(プロパティ1件含む)+ `--strategy smc_bos`のCLI解決確認。depth50 ビット一致再確認済み) |
| **S3** | XAUUSD M30 データ export → 全期間バックテスト → `reports/smc_bos_full/`。出典との乖離分析 | 低(データ依存) | **完了**(2026-06-20。`data/xauusd_m30.parquet`(5年・59,142本)をMT5から取得。PF1.456/197トレード/DD$911.78/勝率31.98%。出典PF2.55は未再現だが優位性は確認。詳細は §1 追記・`reports/README.md`) |
| **S4** | `be_mode=at_1r`/`structure`(SL前進)実装 + SL単調性プロパティテスト。再検証 | 中 | **完了**(2026-06-20。`SmcOutput`新設(signals.py)・`_advance_sl_to`(no-op式・例外なし)。単体11件追加(プロパティ1件含む)。実測比較で `off`(既定)が `at_1r`/`structure` を一貫して上回り、`be_mode` 既定を `off` で確定。`at_1r`/`structure` は実装・テスト済みのopt-in。詳細 §3.3) |
| **S5(任意)** | HTF バイアス / CHoCH / トレーリング / ニュース・セッションフィルタ | 中 | **見送り**(2026-06-20、ユーザー判断)。S0〜S4でコア実装・実測検証・§11全論点の解決が完了しており、これを smc_bos 導入の完了点とする。ニュース/セッションフィルターは Narrow Focus 側でも同様に未実装(経済カレンダーという外部データソース依存のため。CLAUDE.md「既知の制約」参照)。残りの拡張(HTFバイアス・CHoCH・トレーリング)は投機的な機能追加であり、必要になった時点で再提案する |

各段階末で「depth50 ビット一致 + pytest 全合格」を再確認してからコミット(CLAUDE.md 定石5)。

---

## 11. レビューで詰めたい論点(推奨つき)

- **論点A(ゲート方針)**: §5.7 の案A(手法非依存パススルーゲート `--ai-client none`)で進めてよいか。**推奨: 案A**(最小・手法非依存・防御層不変。phase2 の積み残しを CLI 化)。
- **論点B(RR目標)**: ~~`rr_target` 既定3.0を2.5/3.0/5.0で比較最適化する~~ → **解決済み(2026-06-20、変更なし)**。`be_mode=off` 既定構成で 2.0〜5.0(粗)・3.25〜4.5(細)の感度分析を実施した結果、PF・純益とも `rr_target` に対して**非単調かつ激しく振動**(過剰適合の典型シグナル)。単発の最良値(`rr=3.75`、PF1.87)を採用するのはオーバーフィットと判断し、**`rr_target=3.0`(既定)を変更しない**。頑健な知見は「`rr=2.0` は明確な赤字(PF0.97)」のみ。構造的理由(単一ポジション制約の建玉期間がTP距離経由で非連続に変化するため)を含め詳細は `reports/README.md`「論点B検証」節。
- **論点C(BE 方針)**: ~~A-4 抵触のため翻訳~~ → **解決済み(2026-06-20、段階S4実測)**。`be_mode=at_1r`/`structure` を実装し XAUUSD M30・5年で実測した結果、**`off`(SL前進なし)が PF・純益のいずれでも一貫して上回った**(PF1.456 vs 1.384/1.042)。原典忠実性より実測結果を優先し、**`off` を既定として確定**。`at_1r`(原典準拠)・`structure` は実装・テストとも完備した opt-in として残置(将来パラメータ最適化や他シンボルでの再評価に備える)。詳細 §3.3。
- **論点D(管理シグナルの受け渡し)**: ~~構造SL前進に必要な「新スイング情報」を `ProviderOutput` の手法固有ペイロードとして渡す形でよいか~~ → **解決済み**。`ProviderOutput`(Narrow Focus 固有語彙)は再利用せず、`strategies/smc_bos/signals.py` に専用の `SmcOutput`(`plans` + `swing_high_int`/`swing_low_int`)を新設して渡す形で実装(§5.1)。L2→L2 の不要な型結合を増やさない選択。
- **論点E(構造検出の置き場所)**: 段階1は手法フォルダ自前実装(§5.4)。L1 昇格は将来課題。**推奨: 自前実装で開始**。
- **論点F(手法名)**: `smc_bos` で確定してよいか(TF はパラメータなので名前に含めない)。代替 `m30_smc` 等。**推奨: `smc_bos`**。

---

## 12. 原典との差異(明示)

depth50 が原典 Narrow Focus に「システム化のための追加/解釈」を加えたのと同様、SMC も本プラットフォームの不変条件に合わせて以下を**意図的に変更**する。再現性検証はこの差異を前提に評価する:

1. **建値化(BE)**: §A-4 の手法スコープ化により「1:1到達でBE」は**原典どおり実装可能**(`be_mode=at_1r`、判定は確定足の価格水準到達で行う。挙動は等価)だが、**実装後の実測(段階S4)で `off`(SL前進なし)に劣後したため既定では使わない**(`at_1r` はopt-in)。原典忠実性より実証結果を優先した意図的な差異。
2. **スイング検出**: 出典EA の単純 N本前 high/low → **確定遅延つきピボット**(ダマシ/リペイント排除、§A-2)。
3. **AIゲート**: SMC は LLM/ルールゲートを通さず**パススルー**(意味的に不要。防御層は別途強制)。
4. **エントリー約定**: 形成中バーのヒゲ抜けでなく**確定足終値**でのBOS判定(§A-2)。
5. **数値表現**: 価格は整数ティック・EMA/ATR は量子化 Decimal(float禁止、§A-5)。
6. **複利/実口座**: 本書スコープ外(デモ検証必須・自己責任。出典の84,277%は参考値)。

---

> **次アクション**: 本ドラフトをレビューいただき、特に §5.6(M30追加)・§5.3(EMA新設)・§5.7(ゲート方針)・§3.3(BE翻訳)に合意が取れれば、段階 S0(前提整備)から着手する。

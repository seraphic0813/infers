# INFERS 作業プラン 2026-07(最終決定版)

> **ステータス: 確定(2026-07-03)** — ユーザー承認済み。実装は本書のタスクID順に行う。
> 本書は [improvement-report-2026-07.md](improvement-report-2026-07.md)(改善レポート=分析・提案の根拠)に基づく**実行タスク定義**。提案の背景・不採用代替案・ポートフォリオ分析はレポート側を参照。
> 初回セッション冒頭の分析原文(確定前ドラフト)は [improvement-plan-2026-07-draft.md](improvement-plan-2026-07-draft.md) を参照。
> 本書はプラットフォーム全体の改善計画であり、各手法のエントリーロジックの正は
> 各手法フォルダの方法論文書(CLAUDE.md の文書優先順位)を変えない。
> 意思決定の要約はセッションメモリ(improvement-plan-decisions-2026-07)にも保存済み。

---

## 0. 確定した意思決定(全タスクの前提)

| ID | 決定 |
|---|---|
| **D1** | 「depth50 ビット一致」=**強い一致**。トレード列(PF 1.487396352 / 624件 / DD $325.77 / 勝率53.37%)+**ゲート統計(CACHE GO3124・NO_GO1034)**の完全一致。全タスクの完了ゲート=これ+`pytest` 全合格(以下「**完了ゲート**」) |
| **D2** | AI層撤去は**ステップ1(LLM実呼び出し系)まで**。`AiGateway`/`VerdictCache`/`rule_judge.py`/`passthrough.py` は不可侵 |
| **D3** | **smc_bos はライブ凍結**。年次フロア(全暦年 PF≥0.9)不合格が確定: 2022=PF0.801(−$357)赤字、2026年6トレードが純益$4,258の60%($2,559)。コード・テスト・レポートは残置。再開条件: (i) 再開研究で平常年PF≥0.9 達成、または (ii) レジーム回帰の根拠+ユーザー再判定 |
| **D4** | atr_trend_scalp は main へマージ済み(PR #14)。v1.1「スワップ0」は矛盾なし(スワップモデル有効・全2,963トレードで発生額0。保有中央値10分・日またぎ1件のみ、と report_data.js で検証済み) |
| **D5** | BTCUSD/USDJPY 展開は将来想定あり・**今は保留** |

### smc_bos 年次分解(D3 の判定根拠。reports/smc_bos_full/report_data.js から算出)

| 年 | トレード数 | PF | 純益 |
|---|---|---|---|
| 2021(6月〜) | 18 | 1.186 | +$103.36 |
| 2022 | 49 | **0.801** | **−$356.82** |
| 2023 | 46 | 1.332 | +$401.78 |
| 2024 | 26 | 1.577 | +$553.60 |
| 2025 | 52 | 1.243 | +$997.82 |
| 2026(〜6月) | 6 | 4.630 | +$2,558.74 |

---

## 実行モデルとエフォートの割り当て方針

既定の実行者は **Sonnet 5**(仕様が明確な実装・スクリプト・文書)。以下の場合に上位へエスカレーションする:
- **Opus 4.8 / effort high**: 執行シミュレーション・削除リファクタリングなど「壊すと静かに間違う」変更の設計と差分レビュー。
- **Fable 5 / adaptive thinking + effort high**: ホットパス変更(T15)・新手法の設計(T16)・実験の採否判断(過剰適合の見極め)。判断の質が結果を支配するタスク。
- レビューは実装と**別モデル(1段上)**で行う(実装=Sonnet なら レビュー=Opus/Fable)。

| タスク | 実装モデル/エフォート | レビュー/判断モデル | 理由 |
|---|---|---|---|
| T2 `/bit-check` | Sonnet 5 / medium | — | 手順が完全に規定済み |
| T3 年次分解ツール | Sonnet 5 / medium | — | 読み取り専用・正解データあり(smc_bos で警告発火) |
| T4 CLAUDE.md 改訂 | Sonnet 5 / low〜medium | Fable 5 / high(規約の整合確認) | 文書だが規約の正に触れる |
| T5 smc_bos 凍結 | Sonnet 5 / low | — | 停止手順+文書追記 |
| T6 LLM層撤去 | Sonnet 5 / high | **Opus 4.8 / high**(削除境界の審査) | 削除の境界(D2 不可侵リスト)の見極めが核心 |
| T7 スリッページ注入 | **Opus 4.8 / high** | Fable 5 / high(感度判定) | SimBroker=執行シムの変更。opt-in 分岐の置き方を誤ると既定経路が汚染される |
| T8 デモ乖離検証 | Sonnet 5 / medium | Fable 5 / high(昇格判定) | 集計は機械的、昇格可否は判断 |
| T9 スイープ・ハーネス | Sonnet 5 / high | Opus 4.8 / medium | ビット一致自己検証の設計が肝 |
| T10 be_mode 対照 | Sonnet 5 / medium | **Fable 5 / high(採否)** | 実験実行は軽いが採否は過剰適合判断 |
| T11 相関測定 | Sonnet 5 / medium | — | 解析スクリプトのみ |
| T12 横断モニタ | Sonnet 5 / medium | Opus 4.8 / medium | 表示のみ・既存制約(読取専用)の維持確認 |
| T13 OPS(watchdog等) | Sonnet 5 / medium | Opus 4.8 / high(再起動安全性) | 誤再起動→二重プロセスが最悪ケース |
| T14 analysis/ 移設 | Sonnet 5 / medium | — | 機械的 git mv+完了ゲート |
| T15 ゲート宣言化 | **Fable 5 / high** | Opus 4.8 / high(差分レビュー) | ホットパス変更+強い一致の維持。本プラン最難 |
| T16 narrow_focus_mtf | 設計=**Fable 5 / high**、実装=Sonnet 5 / high | Fable 5 / high(採否) | 新手法の設計判断が支配的。spec 確定後の実装は Sonnet |
| T17 開発パイプライン | Sonnet 5 / medium | — | 文書+Skill 定型化 |

---

## Phase 1: 検証インフラと即効改善(Week 1)

### T1: atr_trend_scalp マージ — ✅完了(2026-07-02、PR #14・ローカル同期済み)

### T2: `/bit-check` Skill 【最優先・他タスクの前提】— ✅完了(2026-07-03)
- **目的**: 完了ゲート(D1)のワンコマンド化。
- **指示**:
  1. `.claude/skills/bit-check/SKILL.md` を作成。手順: ① `pytest` 全件 → ② depth50 再現コマンド
     `python -m infers.main --mode backtest --data data/xauusd_m5.parquet --verdict-cache work/cache/verdicts_depth50.sqlite3 --macro-wave2 --depth-screen --depth-max 0.50 --no-fib-score`
     → ③ 出力から PF/トレード数/DD/勝率/ゲート統計を抽出し黄金値(D1 の5値)と文字列レベルで突合 → ④ PASS/FAIL と差分を報告。
  2. 黄金値は Skill 内にハードコード。更新は意図的な基準変更時のみ・理由の記録必須、と Skill 本文に明記。
  3. 実行時間約25分を冒頭に注記(短縮版は作らない。コミット前はフル必須)。
- **完了条件**: 現状 main で PASS。**依存**: なし。**工数**: 0.5日。
- **実績**: `tools/bit_check.py` + `.claude/skills/bit-check/SKILL.md`。黄金値は実測で確認
  (trades=624 / PF=1.487396352 / win_rate=0.533653846 / max_dd=32577 tick*steps($325.77) /
  gate={CACHE:GO=3124, CACHE:NO_GO=1034})。以後 T6 の完了ゲートで実際に PASS 判定に使用。

### T3: 年次分解ツール + `/annual-breakdown` Skill — ✅完了(2026-07-03)
- **指示**:
  1. `tools/annual_breakdown.py`: `report_data.js` から暦年別(`exit_time` UTC 基準)のトレード数/PF/勝率/純益/DD を出力。
  2. 機械警告2つ: (a) 単年純益寄与>50%、(b) いずれかの暦年 PF<0.9。警告時は「過剰適合/一過性依存の疑い」と明記。
  3. Skill `/annual-breakdown <report_dir>` としてラップ。
- **完了条件**: rule_depth50 / smc_bos_full / atr_trend_scalp_full の3つで実行し、smc_bos で警告(a)(b)が両方発火(既知の正解)。トレード判定コード不変。
- **依存**: なし。**工数**: 0.5〜1日。
- **実績**: smc_bos で(a)(b)両方発火(2022年PF0.801・2026年純益寄与60.1%)・atr_trend_scalpで警告なし
  (既知の正解と一致)。depth50でも(b)が2021年(PF0.599)で新規発火 — 追加所見として記録
  (depth50はビット一致制約下ではコード変更不可のため対応不要。新変種[T16]の判断材料)。

### T4: CLAUDE.md 改訂 — ✅完了(2026-07-03)
- **指示**: ① A-12〜15 を「トレード判定に LLM 不使用」の現行方針へ書き換え(旧規約は再導入時の凍結規約として付録へ)。② 黄金値5点(D1)を本文に明記し `/bit-check` を参照。③ 定石6追加「採否は全期間1本で判断せず年次分解+事前宣言基準で判断」。④ 手法テーブル追加(depth50=コア防御/atr=コア攻撃・デモ検証中/smc_bos=**ライブ凍結**/market_tpsl=実証用・運用非対象)。⑤ 「手法はマージされて初めて登録済み」注記。⑥ コマンド表に atr 追加(judge/replay 行の撤去は T6 後)。
- **依存**: T2/T3。**工数**: 0.5日。

### T5: smc_bos ライブ凍結の実施と文書化 — ✅完了(2026-07-03)
- **指示**: ① 2口座目の smc_bos ライブプロセスを stop_live 手順で停止、MT5 建玉なしを確認。② `smc_bos/spec.md` 冒頭に凍結ステータス+判定根拠(本書 §0 の年次テーブル)を追記。③ `reports/README.md` smc_bos 節に年次分解と判定を追記。④ live-startup-runbook の smc_bos 手順を「凍結中」注記つきへ。再開条件は D3 のとおり明記。
- **依存**: なし。**工数**: 0.5日。
- **実績**: spec.md・reports/README.md・live-startup-runbook.md・scripts/start_live.ps1/.bat
  (gitignore対象・ローカルのみ)を更新。実プロセス停止は運用者側の作業として案内(このセッションでは
  MT5プロセスへの直接操作は行っていない)。

---

## Phase 2: 核心改善(Week 2〜4)

### T6: AI層ステップ1撤去(LLM実呼び出し系のみ)— ✅完了(2026-07-03)
- **削除**: `ai/batch.py` / `gateway.py` の `AnthropicLlmClient` / `main.py` の `--mode judge`(--batch/--submit/--fetch/--ingest)と `CacheOnlyClient`・2パス分岐 / 対応テスト(LLM・Batch 依存部分のみ。キャッシュ・ゲートウェイ・ガードレールのテストは残す)。
- **不可侵(D2)**: `AiGateway` 本体・`VerdictCache`・`EscalationPolicy`・`Verdict`/`JudgementRequest`・`rule_judge.py`・`passthrough.py`・`core/loop.py` のゲート呼び出し。1バイトも変えない。
- `--mode replay` は backtest の別名で温存。`anthropic` の必須依存解除を確認。phase1 §7 は「凍結仕様」注記を付け残置。
- **完了条件**: 完了ゲート(強い一致)+削除機能への参照が grep で空。**依存**: T2。**工数**: 2〜3日。
- **実績**: `ai/batch.py`削除・`AnthropicLlmClient`/`CacheOnlyClient`/`DEFAULT_POLICY`/
  `_load_system_prompt`削除・`--mode judge`と関連引数(`--batch`/`--tier`/`--submit`/`--fetch`/
  `--save-raw`/`--ingest`/`--limit`/`--system-prompt-file`)削除・`--ai-client`選択肢から`claude`削除。
  `dashboard/controller.py`の`_build_gateway`呼び出し(シグネチャ変更分)も追随修正。
  pytest 408件全合格 + depth50強いビット一致(PF1.487396352/624件/DD$325.77/勝率53.37%/
  CACHE:GO=3124・NO_GO=1034、`/bit-check`で実測PASS確認)。`anthropic`はpyproject.tomlに
  元々正式依存として未記載だったため依存関係ファイルの変更は不要だった。

### T7: atr_trend_scalp スリッページ・ストレステスト【実口座昇格の関門1】— ✅完了(2026-07-03)
- **指示**: ① SimBroker/engine に opt-in `--slippage-ticks N`(既定0=現行と完全同一経路)。SL/トレール/成行決済を不利方向へ N ティック。② atr 全期間で N=0/1/2/3 の感度テーブルを health_check へ追記。③ **事前宣言基準**: N=2 で PF<1.2 → v1.1 の trail=0.15 は幻とみなし trail 再探索(T9)を起票。PF≥1.2 → T8 へ。
- **完了条件**: 完了ゲート(N=0 で atr レポート再生成が v1.1 値と一致)+感度テーブル文書化。**依存**: T2。**工数**: 2〜3日。
- **実績（判定: PASS → T8 へ。T9 は起動しない）**: `SimBroker.slippage_ticks`(既定0)を成行参入・
  成行決済・保護SL/トレール決済の3約定点に注入(指値は契約上対象外)。`--slippage-ticks` CLI 追加。
  感度実測(全 N でトレード数2,963不変=約定コストのみの純粋劣化):

  | N | PF | 勝率 | 最大DD | 純益 |
  |---|---|---|---|---|
  | 0(=v1.1) | 1.550419248 | 59.91% | $71.02 | +$2,036.92 |
  | 1 | 1.511797159 | 59.37% | $71.86 | +$1,918.40 |
  | **2** | **1.474068065** | 59.13% | $72.78 | +$1,799.88 |
  | 3 | 1.437234751 | 58.56% | $73.78 | +$1,681.36 |

  **N=2 で PF=1.474 ≥ 1.2 → 基準クリア**。全暦年 PF>1.2 を N=3 まで維持(年次分解警告ゼロ)。
  trail=0.15 は理想約定の幻ではない。新規テスト `tests/test_slippage.py` 10件(N=0のno-op・
  各約定点の不利方向スリップ・指値非対象・PF単調悪化プロパティ)。pytest 418件全合格 +
  depth50強いビット一致(N=0既定経路の不汚染)を確認。詳細は
  `reports/atr_trend_scalp_full/health_check.md`「スリッページ・ストレステスト」節。

### T8: atr_trend_scalp デモ約定検証【昇格の関門2】— 🟡ツール実装完了(2026-07-03)・デモ運用は未実施
- **指示**: ① 3口座目でデモ稼働(最低2週間)。② ジャーナル決済イベント vs SimBroker 期待約定価格の乖離集計スクリプト(`tools/`。トレール決済限定の分布: 中央値/p90)。③ 実測乖離を T7 に注入した PF を最終判定値に。④ 実口座昇格基準を文書化(デモ2週間+乖離 p90 が T7 合格ライン内)。
- **依存**: T7 合格。**工数**: 実装1〜2日+運用2週間。
- **実績（②③④は完了。①は運用作業のため未実施）**: `tools/atr_demo_deviation.py` を実装。
  ジャーナルの `SL_HIT` イベント(`mt5_adapter.py`が記録するMT5実約定`deal.price`)と直近の
  `MOVE_SL`/`MARKET_ENTRY`のSL価格を突合し乖離(ticks)を算出。`TP_CLOSE(level=tp1)`の先行
  有無でトレール関連(RUNNER状態)/初期SL(OPEN状態)を分類し、前者をT7フィードバック対象と
  する。p90がT7実測範囲(0〜3tick)内ならその場でPF判定、超過ならT7スイープ拡張を促す設計。
  テスト8件全合格。実口座昇格基準を`reports/atr_trend_scalp_full/health_check.md`
  「デモ約定検証ツール + 実口座昇格基準」節に事前宣言(デモ2週間+乖離p90がT7合格ライン内、
  SL_HITサンプル数n<20は参考値に留める等)。pytest 426件全合格(トレード判定コード不変のため
  depth50完了ゲートの再実行は省略)。**残作業: 3口座目での実デモ2週間稼働+本ツールでの
  乖離実測+最終昇格判定(運用作業。ユーザー側で実施)**。

### T9: スイープ・ハーネス正式化 — ✅完了(2026-07-04)
- **指示**: atr v1.1 のインプロセス・スイープを `tools/sweep.py` へ昇格(バックテスト本体から import されない一方向依存)。必須: (a) 実行前にベースラインをビット一致再現して自己検証、(b) 各構成に年次分解を強制出力(T3 再利用)、(c) 隣接構成間の非単調・振動を検出したら「過剰適合疑い」を明記し採用推奨を出さない。Skill `/sweep` 化。
- **完了条件**: atr v1.1 の既知スイープ結果を再現+完了ゲート。**依存**: T2/T3。**工数**: 3〜5日。
- **実績**: `tools/sweep.py` を実装。`--provider-class`(直接instantiate)+`--strategy`
  (registryからexecution_factoryのみ取得)+`--base`/`--param`(OFAT/grid)の汎用CLI。
  `main._build_gateway`/`_build_swap_config` を直接再利用(二重実装回避・一方向依存を構造的に
  担保)。`_LightRecorder` で年次分解に必要な最小情報のみ収集(HTML描画用の詳細は集めず高速)。
  `detect_oscillation`(隣接値間の符号反転2回以上で過剰適合疑い)を実装。
  **実データ(5年)での再現検証**: ベースライン自己検証がv1.1黄金値と完全一致
  (PF1.550419248/2963件/DD7102)。`--base tp1=1.0,tp2=2.0,trail=0.5`(v1.0)+
  `--param vol_mult=1.2,1.6` のスイープで `vol_mult=1.2` がv1.0黄金値
  (PF1.173164608/6656件/DD25783)と1バイトも違わず一致し、`vol_mult=1.6`でDD半減方向の
  改善(PF1.229/DD12092)を確認(health_check.mdの既知所見をハーネスが正しく再現)。
  テスト22件(`market_tpsl`+合成データで実データ非依存に検証)。pytest 448件全合格
  (既存426+新規22)。トレード判定コード不変のためdepth50完了ゲートの再実行は省略。

### T10: atr_trend_scalp be_mode 対照実験 — 🔲未着手
- T9 上で「建値化なし/トレールのみ」等を全期間+年次分解で対照。採否基準(事前宣言): 全暦年 PF>1.35 の現行フロアを下回る変更は不採用。**依存**: T9(完了済み・着手可能)。**工数**: 0.5日。
- **状態**: 実施痕跡なし(コミットなし、health_check.md に対照実験節なし)。見送り決定はしておらず、単純に未着手。

### T11: 手法間相関の測定(PT-1)— ✅完了
- 3手法の report_data.js から日次損益系列 → ペア相関・コンボDD・最悪同時DD日を算出(`tools/`)。結果は reports/README.md に「ポートフォリオ」節として追記。**依存**: T3。**工数**: 1〜2日。
- **実績**: `tools/portfolio_correlation.py` を実装(コミット 8f0a51d)。3手法の日次損益相関・等倍単純合算ポートフォリオの合成DD・最悪同時損失日を算出し、`reports/README.md`「ポートフォリオ」節に結果を追記。

### T12: 口座横断モニタ(PT-2)— ✅完了
- dashboard に全口座合算(XAUUSD 建玉合計・当日合算実現損益・各キルスイッチ状態)の集約パネル。**表示・警告のみ、自動アクション禁止**。読取専用・127.0.0.1・標準ライブラリのみの既存制約維持。**工数**: 2〜3日。
- **実績**: dashboard に口座横断サマリパネルを追加(コミット ac8d15e)。全口座のXAUUSD建玉合計・当日合算実現損益・各ジャーナルのキルスイッチ状態を集約表示。表示・警告専用で自動アクションなし、既存の読取専用・127.0.0.1・標準ライブラリのみの制約は不変。CLAUDE.mdのdashboard節にも反映済み。

### T13: 運用堅牢化(OPS-1〜3)— ✅完了
- ① ウォッチドッグ(PC再起動後の自動起動。**キルスイッチ作動記録がある口座は自動起動しない**・PID個別管理)。② ハートビート(live→ジャーナル定期追記+ダッシュボード無音警告)。③ 日次リコンサイル(ジャーナル vs MT5 履歴の突合レポート。表示のみ・自動修復しない)。**工数**: 計3〜4日。
- **実績**(コミット ec27409): ①`scripts/register_watchdog_task.ps1`(タスクスケジューラ登録・ローカル実行専用)+`scripts/start_live.ps1`(キルスイッチ作動記録のある口座を自動起動しないガード)。②`LiveRunner`に5分間隔ハートビートの独立スレッドを追加、`HEARTBEAT`ジャーナルイベント記録+ダッシュボードの無音警告(silence_seconds>900秒)。③`tools/daily_reconciliation.py`(ジャーナルの退出イベント件数とMT5実約定履歴を突合、表示のみ・自動修復なし)。キルスイッチ作動検知は`KILL_SWITCH_ENGAGED`ジャーナルイベントとして記録。`docs/live-startup-runbook.md`にOPS-1〜3の運用手順を追記。

### T14: `analysis/` の narrow_focus 移設 — ✅完了(2026-07-04)
- `analysis/dow.py`・`support_resistance.py` を `strategies/narrow_focus/` へ git mv(ロジック不変)。旧パスは PEP 562 遅延 re-export。**完了条件**: 完了ゲート+L0/L1 で `from infers.analysis` が空。**依存**: T2。**工数**: 0.5〜1日。
- **実績**: `git mv` で物理移設(内容無変更)。narrow_focus内の参照元(execution.py・provider.py・
  signals.py・future_discretion.py)のimportを新パスへ更新。旧パス(`infers.analysis.dow`・
  `.support_resistance`・`infers.analysis`本体)は`execution/sm.py`と同型のPEP 562
  `__getattr__`遅延re-exportシムで互換維持(既存テストは旧パスのまま無改修で全合格)。
  `grep "from infers.analysis"` はL0/L1(core/indicators/execution/backtest/data/ai/
  dashboard)で空を確認。副次的発見: dow.py/support_resistance.pyは移設前から
  `strategies.narrow_focus.zigzag.SwingPoint`を参照するL2依存を実際には抱えており
  (phase2-architecture.md 2.0の既知境界違反が未解消のまま残っていた)、本移設で解消。
  docs/phase2-architecture.md の該当節を「解消済み」に更新。
  pytest 488件全合格 + depth50強いビット一致(PF1.487396352/624件/DD$325.77/
  CACHE:GO=3124・NO_GO=1034、`/bit-check`で実測PASS確認)。

---

## Phase 3: 構造課題と Agentic 化(Month 2〜)

### T15: ゲートの手法宣言化(AI層ステップ2)【本プラン最難】— ✅完了(2026-07-04)
- `StrategySpec.gate`(既定 `None`)を追加。`None` の手法は TradingLoop がゲートを呼ばない。narrow_focus/depth50 は現行ルールゲートを宣言し、**呼び出し列・キャッシュキー・統計カウントを完全一致**させる。完了後、非NF手法の signals.py からプレースホルダ捏造フィールドを撤去。
- **完了条件**: 完了ゲート(強い一致が核心)+ゴールデンリプレイ+非NF手法のトレード列がパススルー時と同一。失敗時は差し戻し。**依存**: T6。**工数**: 3〜5日。
- **実績(設計=Fable 5[docs/t15-gate-declaration-design.md]/実装=Sonnet 5/レビュー=Fable 5)**:
  `StrategySpec.gate`("rule"|None、既定None。narrow_focus/depth50のみ"rule")・
  `TradingLoop(gateway=None)` 許容(on_candleの変更はNoneガード2箇所のみ、非None経路は
  1バイト不変)・`resolve_ai_client`(明示CLI>手法宣言>レガシー既定"rule")を実装。
  新規テスト20件(計508件全合格)。検証: ①depth50強いビット一致(PF1.487396352/624件/
  CACHE:GO=3124・NO_GO=1034)実測PASS ②`--strategy depth50 --last 6m` 実装前後で
  stdout**バイト一致** ③atr `--ai-client none`明示も**バイト一致**(後方互換)
  ④atrフラグ無し(新経路=ゲート呼び出しなし)はトレード数59/PF/純益が完全一致・
  ゲート統計行のみ非表示(意図された差)。プレースホルダ物理撤去は見送り
  (`request`はレポートの特徴量スナップショット運搬役として意味が残る/
  `cluster_score`等は`collect_judgements`汎用経路が参照するため。設計書§2.5の
  スコープ限定どおり。L0がこれらを参照するのはゲートブロック内のみ→gate=Noneで
  実行時参照ゼロ、が本質)。phase2 §段階2.5の既知課題を「縮退」で解消。

### T16: `narrow_focus_mtf` 変種(最重要の戦略実験)— ✅完了・**判定: 不採用(参考記録)**(2026-07-04)
- depth50 を一切変更せず、H1 判定足+D1 マクロ(M5 内部リサンプル。`TfResampler` パターンを複製から開始)の新手法として実装・registry 新規登録。原典意図(90SMA=日足スケール・判定TF=H1)との対応表を spec.md に文書化。**採否基準(実装前に宣言)**: 全暦年 PF>1.2 かつ買い側黒字。M15 実験(PF0.778)の劣化前例があるため成績向上を前提にしない。
- **完了条件**: 完了ゲート+`reports/narrow_focus_mtf_full/`+年次分解+採否文書。**依存**: T2/T3/T9。**工数**: 2〜4週。
- **実績(設計=Fable 5[narrow_focus/mtf-variant.md]/実装=Sonnet 5/採否=Fable 5)**:
  M1スコープ(純プリセット+M5→H1決定論リサンプル。provider/execution不変)を実装・実測。
  `tools/resample_parquet.py`(テスト10件)+レジストリ1件(gate="rule"、テスト6件)。
  実測: 80トレード / PF **0.670** / 勝率21.25% / 純益**−$418.71** / 年次フロア割れ4年
  (2021=0.000・2022=0.720・2025=0.126・2026=0.000)/ 買いPF0.421赤字。
  **事前宣言基準に両方未達 → 不採用(参考記録)。レジストリ残置・ライブ投入なし**。
  主要知見: ①売り偏重(86.25%)はTF構造では解消しない=原因はゲート構造 ②BE到達率11.25%
  (depth50=47.6%)— M5較正FSM設定のH1不整合が利確パイプラインを停止(最大の構造要因と推定)
  ③トレードスパン中央値10-12時間(M5比10倍だが原典意図の数日〜週には未達)。
  M2(FSMのH1スケーリング等)は保留(ユーザー判断)。詳細は mtf-variant.md §4。
  pytest 524件全合格 + depth50強いビット一致PASS。

### T17: 開発サブエージェント・パイプライン — ✅完了(2026-07-04)
- 「最適化(/sweep)→アナリスト(年次分解)→インバリアント・レビュアー(A-1〜15+B契約チェックリスト)→/bit-check」の直列パイプラインを定型化。チェックリスト(float混入/形成中バー参照/SL逆行パス/生dict/確定足違反)を `.claude/` 配下に文書化。トレード判断のホットパスには一切関与しない。**工数**: 2〜3日。
- **実績**(PR #22、コミット 410fbe5): `.claude/skills/dev-pipeline/SKILL.md`(実験→年次分解→インバリアント・レビュー→完了ゲートの直列4段パイプライン。各段の入出力・次段へ進める条件・採否基準事後変更禁止・並列多重化禁止を明記)+`.claude/review-checklist.md`(A-2〜A-10不変条件+B契約+depth50不可侵の確認観点・よくある違反例・grep等の確認コマンド例)を新設。CLAUDE.md「新しい手法を足すときの定石」に定石7として参照を追記。T16をこのパイプラインの実例として引用。トレード判断のホットパスは無変更(pytest 524件全合格+depth50強いビット一致で確認済み)。

### T18(保留・D5): BTCUSD/USDJPY 展開 — 着手しない。T16 後にユーザー再判断。
### (降格)SMC-2/3: smc_bos 再開研究 — 優先度最低。実施時は T9 上で trailing 出口+ATRレジームフィルタを実験し、再開条件(平常年PF≥0.9)到達のみを合否とする。

---

## スケジュール総覧

| 週 | タスク | マイルストーン |
|---|---|---|
| **W1** | T2→T3→T4→T5 | /bit-check PASS・年次分解稼働・CLAUDE.md 現行化・smc_bos 凍結完了 |
| **W2** | T6・T7(並行可) | LLM層撤去・atr スリッページ感度判明 |
| **W3** | T8開始(デモ2週)・T9・T14 | ハーネス稼働・analysis/ 移設 |
| **W4** | T10・T11・T12・T13 | be_mode 結論・相関行列・横断モニタ・ウォッチドッグ |
| **W5〜6** | T8判定・T15 | atr 実口座昇格可否・ゲート宣言化 |
| **W7〜10** | T16・T17 | narrow_focus_mtf 実測+採否・開発パイプライン定着 |

**全タスク共通の完了ゲート**: `pytest` 全合格 + depth50 強いビット一致(PF 1.487396352 / 624 / DD$325.77 / 53.37% / CACHE GO3124・NO_GO1034)= `/bit-check` PASS。

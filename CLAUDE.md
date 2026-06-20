# CLAUDE.md — INFERS 開発ガイド

このファイルは Claude Code が本プロジェクトで作業する際に常に参照する開発規約。

**本システムは「複数のトレード手法を載せ替え可能なプラットフォーム」**(フェーズ2完了)。Narrow Focus 手法はその**第1号かつ基準実装**(v1.0 ベースライン = `depth50`)であって、システムそのものではない。新しい手法を足すときは既存手法(特に Narrow Focus / depth50)の挙動を1ビットも変えないこと(`pytest` 全合格 + depth50 バックテストのビット完全一致で担保)。

文書の正の優先順位:
- 基盤の詳細設計は [docs/phase1-architecture.md](docs/phase1-architecture.md)(以下「設計書」)が正。プラットフォーム化(6層化・手法/執行の抽象化)の差分は [docs/phase2-architecture.md](docs/phase2-architecture.md) が正。本書と矛盾した場合は各設計書を優先しつつ本書を更新する。
- **各手法のエントリー判定ロジックは、その手法フォルダ内の方法論文書がその手法内で最優先の正**。Narrow Focus は [narrow_focus/entry-methodology.md](src/infers/strategies/narrow_focus/entry-methodology.md)(設計書の旧エントリー記述より優先)。
- **手法固有のもの(方法論文書・バックテストレポート・固有インジケーター)は各手法フォルダ `src/infers/strategies/<手法名>/` 配下に置く。** `docs/` はアーキテクチャ全体の文書のみ。

## プロジェクト概要

- **システム名**: INFERS (Intelligent Narrow Focus Elliot Realtime System) — 名称は第1号手法 Narrow Focus に由来(固定)
- **目的**: 複数のトレード手法を Python 自動取引Bot+長期バックテストとして実装・比較できるプラットフォーム。各手法は自前の分析(`SignalProvider`)と執行ライフサイクル(`ExecutionModel`)を差し替えられる
- **対象**: BTCUSD (Vantage Trading)、XAUUSD (Swift Trader) — いずれも MT4/MT5系ブローカー
- **アーキテクチャ**: 6層 — L0 core(手法非依存の実行コア・ブローカー/執行抽象)/ L1 indicators(汎用インジケーター)/ L2 strategies(手法ごとの分析+執行)/ L3 backtest / L4 frontend / L5 live。手法は `strategies/registry.py` の `StrategySpec` で名前登録し `--strategy <名前>` で選択。**L0 は手法固有の語彙を一切持たない**(`TradingLoop` は `ExecutionModel` 抽象のみに依存)。詳細は phase2 設計書
- **登録済み手法**: `narrow_focus` / `depth50`(基準実装・既定執行モデル)、`market_tpsl`(SMAクロス+成行/固定TP-SL。執行抽象が手法非依存であることの実証用に、Narrow Focus とは別の執行ライフサイクルを持つ)
- **Narrow Focus 手法のエントリー手法(正は [narrow_focus/entry-methodology.md](src/infers/strategies/narrow_focus/entry-methodology.md))**: 3ゲート構成 — ①マクロ環境認識(**ダウ理論(D1/H1)で方向を確定**、200SMA位置で確認。逆方向を100%遮断)→ ②コンフルエンス(ダウ順行・**200SMAグランビル**・90SMAグランビル・水平レジサポ・**RSIマルチTF(M5/H1/D1)**・**40%深さスクリーニング** が規定数以上重複)→ ③ニュース遮断(重要指標±30分は全面見送り)。エリオット波動は第2波の構造文脈、フィボは**押し目ゾーン特定+利確目標161.8%**(エントリー加点にはしない)。40%深さは原典に無いシステム化の追加フィルター。**この節は Narrow Focus 手法の契約であって、他手法には適用されない**

## 開発環境

- OS: Windows 11(シェルは PowerShell 5.1。`&&` 不可、`;` か `if ($?)` で連結)
- Python 3.12+(venv: `python -m venv .venv` → `.venv\Scripts\Activate.ps1`)
- 主要依存: `MetaTrader5`(Windows専用・ライブ/ヒストリカル/発注)、`numpy`/`polars`、`pydantic` v2、`anthropic`(Messages + Batches)、`duckdb`+Parquet、`structlog`、`pytest`+`hypothesis`
- パッケージレイアウト: `src/infers/`(phase2 設計書の6層構成に従う。新モジュールを勝手な場所に作らない。手法固有コードは `strategies/<手法名>/`、複数手法で再利用する汎用インジケーターのみ `indicators/`)
- APIキー・口座資格情報は環境変数のみ。コード・設定ファイル・ログ・ジャーナルに書かない

## 新しい手法を足すときの定石

1. `src/infers/strategies/<手法名>/` を作る(`__init__.py` は循環 import を避けるため即時 re-export しない)。分析は `provider.py`(`SignalProvider`: `on_candle(candle) → ProviderOutput`)、執行が Narrow Focus と異なるなら `execution.py`(`ExecutionModel` プロトコル)。
2. その手法専用の計算は手法フォルダ内に置く。**複数手法で使うようになった汎用インジケーターのみ** `indicators/` へ吸い上げる。
3. `strategies/registry.py` に `StrategySpec` を1件 `register(...)`。執行を差し替えるなら `build_execution=` を指定、Narrow Focus 既定でよければ `None`。
4. 手法の方法論文書・バックテストレポートはその手法フォルダ配下に置く(`docs/` に置かない)。
5. **既存手法の挙動を変えていないこと**を `pytest` 全合格 + depth50 バックテストのビット完全一致で確認してからコミットする。

## 主要コマンド(2026-06-20 実装に合わせて確定)

全モードは単一エントリ `python -m infers.main --mode <mode> ...` から実行する
(`pyproject.toml` の `[project.scripts]` 登録は将来課題。詳細は `infers.main` のモジュール
docstring)。手法は `--strategy <名前>`(レジストリ登録名)で選択。`--provider 'module:attr'`
で任意の `SignalProvider` を直接差し込むこともできる:

| コマンド | 用途 |
|---|---|
| `pytest` / `pytest -m property` | 単体テスト / hypothesis プロパティテスト(状態機械の不変条件)。331件合格(2026-06-20) |
| `python -m infers.main --mode backtest --data <parquet> [--strategy <名前>] [--report DIR] ...` | L0決定論スイープ+ルールベース($0)/キャッシュ済みverdictでのバックテスト。v1.0確定構成は `reports/README.md` 再生成コマンド参照。期間スライスは `--from`/`--to`/`--last` |
| `python -m infers.main --mode judge --data <parquet> --batch [--tier L1\|L2]` | Pass1: 裁定イベントをBatch APIリクエスト(JSONL)に書き出す |
| `python -m infers.main --mode judge --submit` / `--fetch <BATCH_ID>` / `--ingest <results.jsonl>` | Batches API投入/結果取得/取込 → `verdict_cache`へ永続化 |
| `python -m infers.main --mode replay --data <parquet> --verdict-cache <db>` | Pass2: verdict_cache参照の最終リプレイ(`backtest`と同一実装) |
| `python -m infers.main --mode live --demo [--symbol XAUUSD] [--max-bars N] [--journal PATH]` | ライブ稼働(デモ必須。実口座は`--allow-real-account`明示が必要)。判断は追記専用ジャーナル(既定 `work/journal/<symbol>_<UTC日付>.jsonl`)へ永続化 |
| `python -m infers.journal replay --file <path> [--from <ts>]` | ジャーナルの要約 + ゴールデン回帰検証(ルールゲートのセッションは記録済み特徴量を `judge_features` へ再投入し同一判断を確認)。実装は `src/infers/journal.py` |
| `python -m infers.main --mode export --data <out.parquet> --years 5` | MT5からヒストリカルデータをParquetへ書き出し |
| `python -m infers.dashboard [--port 8765] [--symbol XAUUSD]` | ローカル監視ダッシュボード(127.0.0.1のみ・標準ライブラリのみ・トレード根幹から分離)。ブラウザから口座情報入力・監視開始/安全停止・ジャーナル監視。内部はlive稼働と同一経路を呼ぶだけ。資格情報はメモリ保持のみ。実装は `src/infers/dashboard/` |

- テストはCIで `pytest` 全件を必須とする。状態機械・注文ロジックの変更はプロパティテスト追加なしにマージしない。
- ゴールデンリプレイ(同一入力→同一判断の回帰検証)は `tests/test_journal.py` でカバー。ルールゲート($0・決定論)のセッションについて、ジャーナルの特徴量スナップショットから判定が再現されることを検証する。LLMゲート(`--ai-client claude`)は非決定論のため対象外。

## コード記述の厳格なルール

設計書 §0「最重要設計原則」の要約+実装規約。**違反するコードは書かない・レビューで通さない。**

規約は「**全手法・プラットフォーム共通の不変条件(A)**」と「**Narrow Focus 手法固有の契約(B)**」に分かれる。A は L0/L1 とすべての手法が必ず守る。B は Narrow Focus 手法だけの定義であり、別ロジックの手法(例: `market_tpsl`・`smc_bos`)は B の一部(コンフルエンス・半分利確・エリオット・**防御調整のトリガー定義**等)を持たなくてよい — ただし A は全手法が無条件に守る。

> **「全手法共通(A)」か「手法固有(B)」かの判定基準**: ある制約が **A** に属するのは、それが「安全性・決定論性・プラットフォーム整合性」を担保し、**どんな戦略でも破ってよい理由が無い**もの(例: SL は利益方向にしか動かさない=A-3)に限る。一方、「**いつ・どの価格で建て、どう手仕舞うか**」という戦術判断は手法の自由であり、そこに含まれる制約(出口のトリガー種別など)は **B(各手法の契約)** に置く。グローバルに縛ると別ロジックの手法を不当に制約してしまうものは A に置かない。

### A. 全手法共通の不変条件(絶対)

#### 安全原則
1. **防御はLLM非依存** — 損切り・SL移動・利確・キルスイッチ等の防御/出口は100%決定論的なPythonで完結。LLMは新規エントリーのゲートのみ。LLM障害・予算超過・parse失敗時のデフォルトは常に NO-TRADE / NO_GO。
2. **確定足主義(リペイント禁止)** — 売買判断は対象TFの確定足クローズ時のみ。スイング・波カウント等は `confirmed_at` 以降のみ判断材料にできる。形成中バーやティックのヒゲで判定しない。
3. **SLの単調性** — SLは利益方向にしか動かさない。買いポジションでSLを下げるコードパスは存在してはならない(hypothesisで担保)。どの手法の `ExecutionModel` でも同様。
4. **防御調整のトリガー種別は各手法の契約とする(グローバルには縛らない)** — SL移動・建値化等の防御調整を「含み益(PnL/pips)で行うか」「価格構造イベントに限るか」は**手法ごとに B 相当の契約として定義**する。グローバル不変条件として全手法に強制するのは **A-3(SL単調性=防御は利益方向にしか動かさない)** のみであり、それを満たす限り防御調整の**トリガー種別は戦略の自由**とする(含み益トリガーそれ自体は安全性を損なわない。早計な建値化で優位性を削るか否かは戦略の選択)。
   - **Narrow Focus(depth50)はこの自由を使わず「価格構造イベント限定(含み益トリガー禁止)」を自手法の契約として課す**(B 参照。`narrow_focus/execution.py` は含み益・pips を受け取るAPIを持たない構造で物理的に担保)。
   - 別ロジックの手法(例: `smc_bos` の「1R 到達で建値化」)は、A-3 を満たす限り含み益ベースのトリガーを**自手法の契約として採用してよい**。採用する手法は、そのトリガー定義を当該手法の方法論文書に明記する。

#### 数値・型規約
5. **float禁止(価格・数量)** — 価格は整数ティック(`price_int`)、数量はロットステップ整数倍で保持。float同士の `==`/`<` 比較は禁止。表示・API境界でのみ Decimal/str へ変換。
6. **時刻はUTC固定** — `datetime` は必ず tz-aware UTC。ローカル時計を判定に使わない。ブローカー時刻オフセットはアダプタ層で吸収。
7. **スキーマ固定** — モジュール間で受け渡すデータは frozen dataclass / pydantic モデルのみ。生dictの引き回し禁止。LLM入出力は `client.messages.parse` + pydantic で強制。

#### 状態・執行規約
8. **状態はEnum一本の有限状態機械** — boolフラグの組合せでポジション状態を表現しない。各手法の `ExecutionModel` は Enum の状態遷移として実装する(Narrow Focus の遷移図は設計書 §6.1)。
9. **冪等性** — 全注文操作に決定論的 `client_order_id` を付与。リトライで二重発注・二重決済が起きない構造にする。
10. **イベントソーシング** — 全判断を特徴量スナップショットとともに追記専用ジャーナルへ記録。「ログに出してない判断」を作らない。
11. **戦略コアは純粋関数** — I/O(フィード・ブローカー・LLM)はアダプタに隔離し、バックテストとライブで同一コードパスを通す。`TradingLoop` は `ExecutionModel` 抽象のみに依存し、特定手法の執行手順を直接呼ばない。

#### AI層規約
12. **LLMにインジケーター計算をさせない** — 渡すのは事前計算済み数値特徴量のみ。生ローソク足・生チャートを渡さない。
13. モデルIDは `claude-haiku-4-5`(L1) / `claude-fable-5`(L2)。Fable 5 は `thinking={"type": "adaptive"}` + `output_config={"effort": "high"}`、sampling パラメータ(`temperature` 等)と明示 `thinking disabled` は400になるので渡さない。
14. システムプロンプト(手法マニュアル+判定ルール)は凍結し `cache_control` でキャッシュ。揮発要素(時刻・乱数)を混ぜない。可変部は `sort_keys=True` でJSON決定論化。
15. バックテストのLLM呼び出しは必ず2パス+Batches API+`verdict_cache`(key=(model, prompt_version, feature_hash))経由。同期ループ内で `messages.create` を直接呼ばない。

> **既知の制約(フェーズ3候補)**: 現状のAIゲート(`rule_judge.py`)は Narrow Focus 固有の特徴量を前提とするため、別手法の plan は GUARDRAIL NO_GO になる。ゲートを手法非依存へ一般化するのが次の論点(防御層は全手法に等しく強制されるため、これは安全性ではなく約定可否の問題)。

### B. Narrow Focus 手法固有の契約(変更禁止の定義)

**この節は `strategies/narrow_focus/`(= depth50 基準実装)にのみ適用される。** 他手法はこれらを持たなくてよい。

- **コンフルエンス必須** — エントリー候補は `distinct_families >= 2` を満たすクラスタ単位でのみ生成(単一根拠エントリーの禁止。設計書 §4.4)。
- **防御調整に含み益トリガーを使わない(本手法の安全契約)** — 本手法では SL移動・建値化等の防御調整を **PnL・pips・含み益を条件に行わない。トリガーは価格構造イベントに限る**。具体機構: 建値SL移動はダウ状態機械の「安値切り上げ/高値切り下げ確定」確定イベントでのみ行う(設計書 §6.3)。これは Narrow Focus(depth50)の**手法固有契約**であり、A-3(SL単調性)とは別物(A-3 は全手法強制、本契約はトリガー種別の制限で depth50 のみ)。別ロジックの手法には課されない(A-4 参照)。
- **エリオット3原則は無効化価格として実装する**(設計書 §3.3)。原則違反の検出はこの価格との比較のみで行う。
- **「第1波高値超え」** = 判定TF確定足終値 > `W1_high + max(α_atr×ATR, n_ticks, spread×m)`(設計書 §6.2)。
- **半分利確** は `half_volume`/`runner_volume` をエントリー時に固定し、厳密に1回のみ(設計書 §6.4)。
- **未来裁量の指値** には必ず `expiry` と `invalidation_price` を付け、毎確定足で再計算する(設計書 §5.5)。

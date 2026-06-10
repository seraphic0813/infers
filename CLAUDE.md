# CLAUDE.md — INFERS 開発ガイド

このファイルは Claude Code が本プロジェクトで作業する際に常に参照する開発規約。
詳細設計は [docs/phase1-architecture.md](docs/phase1-architecture.md)(以下「設計書」)が唯一の正であり、本書と矛盾した場合は設計書を優先しつつ本書を更新すること。

## プロジェクト概要

- **システム名**: INFERS (Intelligent Narrow Focus Elliot Realtime System)
- **目的**: 「Narrow Focus トレード手法」(ダウ理論+エリオット波動+フィボナッチのマクロ分析 × 90/200SMA・RSI・レジサポのミクロ分析、および未来裁量)を Python 自動取引Bot+長期バックテストとして実装する
- **対象**: BTCUSD (Vantage Trading)、XAUUSD (Swift Trader) — いずれも MT4/MT5系ブローカー
- **構成**: データ層 → 分析層(マクロ/ミクロ/未来裁量) → シグナル統合 → AIゲート(L1/L2) → リスクマネージャー → 執行。詳細は設計書 §1

## 開発環境

- OS: Windows 11(シェルは PowerShell 5.1。`&&` 不可、`;` か `if ($?)` で連結)
- Python 3.12+(venv: `python -m venv .venv` → `.venv\Scripts\Activate.ps1`)
- 主要依存: `MetaTrader5`(Windows専用・ライブ/ヒストリカル/発注)、`numpy`/`polars`、`pydantic` v2、`anthropic`(Messages + Batches)、`duckdb`+Parquet、`structlog`、`pytest`+`hypothesis`
- パッケージレイアウト: `src/infers/`(設計書 §1.2 のディレクトリ構成に従う。新モジュールを勝手な場所に作らない)
- APIキー・口座資格情報は環境変数のみ。コード・設定ファイル・ログ・ジャーナルに書かない

## 主要コマンド(方針)

実装が進むまでは以下の規約でCLIを整備する(`python -m infers.<cmd>` 形式、エントリは `pyproject.toml` の scripts に登録):

| コマンド | 用途 |
|---|---|
| `pytest` / `pytest -m property` | 単体テスト / hypothesis プロパティテスト(状態機械の不変条件) |
| `python -m infers.backtest run --config config/bt.yaml` | Pass1: L0決定論スイープ(設計書 §7.4) |
| `python -m infers.backtest judge --batch` | Pass2: 裁定イベントを Batches API へ投入し verdict_cache に永続化 |
| `python -m infers.backtest replay` | Pass3: verdict_cache 参照の最終リプレイ+メトリクス出力 |
| `python -m infers.journal replay --from <ts>` | ジャーナルのゴールデンリプレイ(同一入力→同一判断の回帰検証) |
| `python -m infers.live --symbol XAUUSD --demo` | ライブ稼働(デモ必須から開始) |

- テストはCIで `pytest` 全件+ゴールデンリプレイを必須とする。状態機械・注文ロジックの変更はプロパティテスト追加なしにマージしない。

## コード記述の厳格なルール

設計書 §0「最重要設計原則」の要約+実装規約。**違反するコードは書かない・レビューで通さない。**

### 安全原則(絶対)
1. **防御はLLM非依存** — 損切り・建値SL・半分利確・キルスイッチは100%決定論的なPythonで完結。LLMは新規エントリーのゲートのみ。LLM障害・予算超過・parse失敗時のデフォルトは常に NO-TRADE / NO_GO。
2. **確定足主義(リペイント禁止)** — 売買判断は対象TFの確定足クローズ時のみ。スイング・波カウントは `confirmed_at` 以降のみ判断材料にできる。形成中バーやティックのヒゲで判定しない。
3. **SLの単調性** — SLは利益方向にしか動かさない。買いポジションでSLを下げるコードパスは存在してはならない(hypothesisで担保)。
4. **建値SL移動は含み益トリガー禁止** — ダウ状態機械の「安値切り上げ/高値切り下げ確定」イベント駆動のみ(設計書 §6.3)。PnLやpipsを条件に使うコードは書かない。
5. **コンフルエンス必須** — エントリー候補は `distinct_families >= 2` を満たすクラスタ単位でのみ生成(単一根拠エントリーの禁止。設計書 §4.4)。

### 数値・型規約
6. **float禁止(価格・数量)** — 価格は整数ティック(`price_int`)、数量はロットステップ整数倍で保持。float同士の `==`/`<` 比較は禁止。表示・API境界でのみ Decimal/str へ変換。
7. **時刻はUTC固定** — `datetime` は必ず tz-aware UTC。ローカル時計を判定に使わない。ブローカー時刻オフセットはアダプタ層で吸収。
8. **スキーマ固定** — モジュール間で受け渡すデータは frozen dataclass / pydantic モデルのみ。生dictの引き回し禁止。LLM入出力は `client.messages.parse` + pydantic で強制。

### 状態・執行規約
9. **状態はEnum一本の有限状態機械** — boolフラグの組合せでポジション状態を表現しない(設計書 §6.1 の状態遷移図に従う)。
10. **冪等性** — 全注文操作に決定論的 `client_order_id` を付与。リトライで二重発注・二重決済が起きない構造にする。
11. **イベントソーシング** — 全判断を特徴量スナップショットとともに追記専用ジャーナルへ記録。「ログに出してない判断」を作らない。
12. **戦略コアは純粋関数** — I/O(フィード・ブローカー・LLM)はアダプタに隔離し、バックテストとライブで同一コードパスを通す。

### AI層規約
13. **LLMにインジケーター計算をさせない** — 渡すのは事前計算済み数値特徴量のみ。生ローソク足・生チャートを渡さない。
14. モデルIDは `claude-haiku-4-5`(L1) / `claude-fable-5`(L2)。Fable 5 は `thinking={"type": "adaptive"}` + `output_config={"effort": "high"}`、sampling パラメータ(`temperature` 等)と明示 `thinking disabled` は400になるので渡さない。
15. システムプロンプト(手法マニュアル+判定ルール)は凍結し `cache_control` でキャッシュ。揮発要素(時刻・乱数)を混ぜない。可変部は `sort_keys=True` でJSON決定論化。
16. バックテストのLLM呼び出しは必ず2パス+Batches API+`verdict_cache`(key=(model, prompt_version, feature_hash))経由。同期ループ内で `messages.create` を直接呼ばない。

### 手法ロジックの正(変更禁止の定義)
- エリオット3原則は**無効化価格**として実装する(設計書 §3.3)。原則違反の検出はこの価格との比較のみで行う。
- 「第1波高値超え」= 判定TF確定足終値 > `W1_high + max(α_atr×ATR, n_ticks, spread×m)`(設計書 §6.2)。
- 半分利確は `half_volume`/`runner_volume` をエントリー時に固定し、厳密に1回のみ(設計書 §6.4)。
- 未来裁量の指値には必ず `expiry` と `invalidation_price` を付け、毎確定足で再計算する(設計書 §5.5)。

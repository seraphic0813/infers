# T15 設計書: ゲートの手法宣言化(AI層ステップ2)

> **ステータス: 設計確定(2026-07-04、Fable 5)/ 実装: Sonnet 5** — 改善プラン
> [improvement-plan-2026-07.md](improvement-plan-2026-07.md) T15。
> レビュー・完了判定は Fable 5 が行う。
>
> **目的**: `TradingLoop` が全プランへゲート呼び出しを強制する現構造を「手法が
> ゲートの要否を宣言する」形へ**縮退**させ、L0 から Narrow Focus 語彙
> (`cluster_score`/`ambiguity`)への依存を消す。phase2 §段階2.5 の既知課題を
> 「一般化」ではなく「縮退」で解消する(改善レポート §2-2 ステップ2)。

---

## 0. 完了ゲート(この設計の絶対条件)

1. **depth50 強いビット一致**(`/bit-check`): トレード列+ゲート統計
   `CACHE:GO=3124 / CACHE:NO_GO=1034` が1バイトも変わらない。
   bit-check は**レガシーCLI経路**(`--strategy` 無し・`--ai-client` 無し)を検証する。
2. **`--strategy depth50` 経路のスポット検証**: 実装**前**に main で
   `--strategy depth50 --last 6m` の出力(trades/PF/DD/gate統計)を採取し、実装後と
   完全一致すること。
3. **非NF手法のトレード列不変**: `--strategy atr_trend_scalp --last 3m` を
   (a) `--ai-client none` 明示(旧経路=パススルー)と (b) フラグ無し(新経路=ゲート
   呼び出しなし)の両方で実行し、**トレード数・PF・純益が完全一致**すること
   (ゲート統計行は (a)=`L1:GO=N` / (b)=表示なし、と異なってよい — 意図された差)。
4. `pytest` 全合格 + ゴールデンリプレイ(`tests/test_journal.py`)合格。

## 1. 現状の問題(再掲)

`TradingLoop.on_candle`(core/loop.py:177)は全プランに対し
`self._gateway.judge(plan.request, cluster_score=plan.cluster_score, ambiguity=plan.ambiguity)`
を無条件に呼ぶ。このため:
- L0 が NF 語彙(`cluster_score`/`ambiguity`/`JudgementRequest`)へ依存する。
- 非NF手法(market_tpsl/smc_bos/atr_trend_scalp)はゲートを意味的に必要としないのに、
  プランへ中立値のプレースホルダを捏造し、`--ai-client none`(常時GOのパススルー)で
  「存在するが何も判定しないゲート」を通している。

## 2. 設計

### 2.1 `StrategySpec.gate` フィールド(registry.py)

```python
@dataclass(frozen=True)
class StrategySpec:
    ...
    # エントリーゲートの宣言 (T15)。None = ゲートなし (TradingLoop はゲートを
    # 呼ばない)。"rule" = rule_judge.py の決定論ルールゲート (verdict_cache 併用)。
    gate: "str | None" = None
```

- `narrow_focus` / `depth50` → `gate="rule"`(現行のルールゲートと**同一の呼び出し列・
  キャッシュキー・統計カウント**を維持する)。
- `market_tpsl` / `smc_bos` / `atr_trend_scalp` → `gate=None`(既定値なので記述不要)。
- (T16 の `narrow_focus_mtf` も `gate="rule"` — 同じ NF 特徴量スキーマを出すため
  ルールゲートが意味的に機能する。)

### 2.2 `TradingLoop`: `gateway: AiGateway | None`(core/loop.py)

- `__init__` のシグネチャを `gateway: AiGateway | None` に緩和(キーワード必須のまま)。
- `on_candle` 内の変更点は**2箇所のみ**:
  1. 日次境界の `self._gateway.new_day()` → `if self._gateway is not None:` ガード。
  2. 新規プランのゲートブロック(judge 呼び出し+VERDICT 記録+`decision != "GO"` の
     continue)全体を `if self._gateway is not None:` で囲む。**None のときはこの
     ブロックを丸ごとスキップ**し、プランは無条件でリスク拒否権へ進む
     (パススルーは常時GOのため、スキップとパススルーはトレード列に対して構造的に等価。
     防御層=リスク拒否権・SL単調性・冪等IDは従来どおり全手法に強制)。
- `gateway is not None` の経路(NF)は**1バイトも変えない**(judge の引数・VERDICT の
  記録内容・順序を含め現状維持)。

### 2.3 `main.py`: `--ai-client` の解決順序

`--ai-client` の argparse 既定値を `"rule"` から **`None`(未指定)** に変更し、
解決ヘルパーを新設する:

```python
def resolve_ai_client(args) -> str | None:
    """エントリーゲート方式の解決。戻り値: "rule" | "none" | None(ゲートなし)。

    優先順位:
      1. 明示された --ai-client(後方互換: 文書化済みコマンドの挙動を完全維持)
      2. --strategy 指定時: レジストリの StrategySpec.gate 宣言
         ("rule" → ルールゲート / None → ゲートなし)
      3. レガシー経路(--strategy 無し / --provider): "rule"(従来の既定)
    """
```

- `_build_gateway` は解決結果を受け取り、`None`(ゲートなし)なら **`None` を返す**。
  `"rule"` / `"none"` は現行どおり `RuleBasedLlmClient` / `PassthroughLlmClient` を配線。
- **後方互換の保証**:
  - depth50 再現コマンド(`--ai-client` 無し・`--strategy` 無し)→ 優先順位3 →
    `"rule"` → 従来と完全一致(bit-check で担保)。
  - `--strategy depth50`(フラグ無し)→ 優先順位2 → `gate="rule"` → 従来と一致。
  - reports/README.md 等の文書化済みコマンド(`--strategy atr_trend_scalp --ai-client none`)
    → 優先順位1 → パススルー → **統計行まで含め従来と完全一致**。
  - 新規実行では `--ai-client` を省略でき、非NF手法はゲート呼び出し自体が消える。

### 2.4 `gateway=None` の波及箇所(main.py / mt5_adapter.py / dashboard)

- `run_backtest`: `gateway` が None のとき `RecordingGateway` で包まない
  (`BacktestRecorder.gateway` は既に `None` 許容 — `_verdict_dict` が None を返す)。
  `_print_gateway_stats` は None なら何も表示しない。
- `run_live` / `LiveRunner` / `dashboard/controller.py`: `gateway=None` をそのまま
  `TradingLoop` へ渡す(2.2 で許容済み)。
- `_open_live_journal` の SESSION イベント: `"ai_client"` フィールドは解決結果
  (`"rule"`/`"none"`/`None`)を記録する。`None` のとき `journal.replay` の
  ゴールデンリプレイは対象外となる(`ai_client == "rule"` のみ対象 — 現行ロジックの
  まま正しく動作する)。
- `dashboard/controller.py` の `_v10_args`(parse_args を再利用)→ `--ai-client` 無し・
  `--strategy` 無し → 優先順位3 = "rule" → 従来と一致。

### 2.5 プレースホルダの扱い(スコープ限定)

- `plan.request`(`JudgementRequest`)は**ゲート専用ではない**: `BacktestRecorder` が
  `plan.request.features` をトレード詳細(HTMLレポート)の特徴量スナップショットとして
  使う。したがって T15 では **`request` を撤去しない**(「特徴量スナップショットの
  運搬役」として意味が残る)。
- `cluster_score` / `ambiguity` はゲート専用。`gate=None` の手法では実行時に一切参照
  されなくなるが、`TradePlan`(NF 語彙)を再利用する market_tpsl / smc_bos からの
  フィールド撤去は TradePlan 本体の変更(=NF 経路へのリスク)を伴うため **T15 では
  行わない**。`AtrTrendPlan`(atr 専用クラス)に同フィールドがあれば、そのクラスから
  のみ撤去してよい(ただし report_html 互換フィールドは残す)。中途半端に見えるが、
  「L0 がこれらを参照するのはゲートブロック内のみ→ gate=None で参照ゼロ」が本質であり、
  語彙の物理削除は任意の後続クリーンアップとする。

### 2.6 やらないこと(明示)

- ゲートの「手法非依存への一般化」(手法が特徴量スキーマ・エスカレーションを宣言する
  本格版)— 改善レポート「代替案」で不採用確定。
- `EscalationPolicy` / `VerdictCache` / `rule_judge.py` / `passthrough.py` の変更(D2 不可侵)。
- `--ai-client` の選択肢変更(rule / none のまま)。

## 3. テスト計画(新規)

1. `resolve_ai_client`: (明示 CLI / strategy 宣言 / レガシー) × (rule/none/None) の
   優先順位テーブルテスト。
2. `TradingLoop(gateway=None)`: プランが judge を経ずにリスク層→発注へ進む /
   VERDICT がジャーナルに記録されない / new_day で例外にならない。
3. `TradingLoop(gateway=あり)`: 既存テストが無改修で全合格(呼び出し列不変の傍証)。
4. registry: 各手法の `gate` 宣言値のアサーション。
5. 統合: `--strategy atr_trend_scalp` フラグ無しで gateway=None が配線される
   (`_build_gateway` 経由)。

## 4. 実装手順(Sonnet への指示の骨子)

1. **実装前にベースライン採取**: main の状態で
   `python -m infers.main --mode backtest --data data/xauusd_m5.parquet --strategy depth50 --last 6m --verdict-cache <scratch>/t15_d50.sqlite3`
   と `--strategy atr_trend_scalp --tf M5 --ai-client none --last 3m` の出力を保存。
2. registry → loop → main → 波及箇所 → テストの順に実装。
3. `pytest` 全合格 → 完了ゲート §0 の 2・3 を検証 → コミット。
4. フル bit-check(25分)はレビュー担当(Fable)が実行する。

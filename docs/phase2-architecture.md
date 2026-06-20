# INFERS フェーズ2 アーキテクチャ設計 — 多手法プラットフォーム化

> **ステータス: ドラフト (レビュー待ち)** — 2026-06-19 起案。
> 本書はユーザーとの設計合意を取るための叩き台。合意後に確定し、段階的移行へ移る。

## 0. この文書の位置づけ

- [docs/phase1-architecture.md](phase1-architecture.md)(以下「phase1」)が引き続き**詳細設計の正**。本書はそれを否定せず、**「単一手法(Narrow Focus)固定」だった構造を「複数手法プラットフォーム」へ一般化する差分**を定義する。
- エントリー判定ロジックの正は引き続き [docs/entry-methodology.md](entry-methodology.md)。本書は Narrow Focus 手法の中身を1ビットも変えない。
- 合意・確定後、[CLAUDE.md](../CLAUDE.md) のディレクトリ構成・コマンド節を本書に合わせて更新する。

## 1. 背景 — なぜ再設計するか

現状コードを精査して判明した制約:

1. **「手法」が単一の巨大パイプライン + パラメータでしかない。** Narrow Focus は [src/infers/strategy/provider.py](../src/infers/strategy/provider.py) の `InfersSignalProvider`(1クラス)に固定実装され、`depth50` 等は `ProviderConfig` のパラメータ違い + 大量のCLIフラグでしかない。**全く別ロジックの手法**(例: 移動平均クロス、ブレイクアウト)を足す枠が無い。
2. **執行手順が Narrow Focus に強く結合。** [src/infers/execution/sm.py](../src/infers/execution/sm.py) の `PositionFSM`(状態: `PROBE_PENDING→PROBE→SL_AT_BE→ADD→RUNNER→CLOSED`)と、それを駆動する [src/infers/core/loop.py](../src/infers/core/loop.py) の `TradingLoop` が、「指値打診→第1波ブレイク追撃→半分利確→ランナー」という**特定の執行モデルを直接呼んでいる**(`on_wave1_break` / `on_half_tp_signal` / `on_runner_target`)。`TradePlan` / `ProviderOutput` も `w1_high_int` / `fib_target_int` / `cluster_score` 等の Narrow Focus 固有語彙を持つ。
3. **良い土台もある。** 手法の注入点 `SignalProvider` プロトコル(`on_candle(candle) → ProviderOutput`)と共通実行コア `TradingLoop` は既に分離済み。`--provider 'module:attr'` で差し替える穴も既にある。安全層(SL単調性 `SLMonotonicGuard`、冪等 `client_order_id`、リスク拒否権 `RiskManager`、追記ジャーナル)も独立している。

## 2. 設計判断の記録 (2026-06-19, ユーザー決定)

| 論点 | 決定 | 含意 |
|---|---|---|
| 新手法の執行モデル | **執行も手法ごとに差替可能** | `ExecutionModel` を抽象化し、現 `PositionFSM` をその一実装にする。最も難度の高い改修 |
| フル/ライトテスト | **5年データ1つから期間切出し** | データは一元管理。`--period` / `--from`・`--to` / `--last` で期間だけ変える |
| 進め方 | **設計を固めてから段階的に** | 本書で合意 → 各段階で depth50 の挙動が1ビットも変わらないことを検証しながら移行 |

**最重要の制約(絶対不変)**: 執行を差替可能にしても、[CLAUDE.md](../CLAUDE.md) §0 の安全原則(防御はLLM非依存・確定足主義・SL単調性・コンフルエンス必須・冪等性・イベントソーシング)は**全手法・全執行モデルに強制され続ける**。自由になるのは「いつ・どの価格で建て、どう手仕舞うか」の戦術だけであり、安全層は共通基盤として全モデルの外側で守る。

## 3. 6層アーキテクチャ

ユーザー提案の5層に、手法にも運用にも属さない**コア層(L0)**を加えた6層とする。`TradingLoop` や安全層は「全手法が共有する実行基盤」であり、特定の手法にも運用にも属さないため独立させる。

```
┌─────────────────────────────────────────────────────────┐
│ L4 フロントエンド層   レポートUI / 監視ダッシュボード         │
├─────────────────────────────────────────────────────────┤
│ L3 バックテスト層     期間スライス・SimBroker駆動・成績集計    │
│ L5 運用層            ライブ実行・MT5アダプタ・AIゲート・永続化  │  ← L3とL5は対称(同じL0/L2を駆動)
├─────────────────────────────────────────────────────────┤
│ L2 手法層            複数手法プラグイン =                     │
│                       Strategy(分析・シグナル) +             │
│                       ExecutionModel(執行ライフサイクル)     │
│                      [depth50 = 最初の登録手法]              │
├─────────────────────────────────────────────────────────┤
│ L1 インジケーター層   汎用部品 SMA / RSI / MACD / ATR ...     │
├─────────────────────────────────────────────────────────┤
│ L0 コア / 基盤層      TradingLoop・データモデル・安全層        │
│                      (RiskManager / SLMonotonicGuard /      │
│                       冪等ID / ジャーナル / BrokerPort)       │
└─────────────────────────────────────────────────────────┘
依存方向: 上位層は下位層にのみ依存する。L0 は何にも依存しない純粋コア。
         L3(バックテスト)とL5(運用)は兄弟で、どちらも L0+L2 を駆動するだけ
         (CLAUDE.md 第12条「ライブ・バックテスト同等性」の構造的保証)。
```

### 3.1 現状モジュール → 新層マッピング

| 層 | 新規/移動先 | 現状モジュール | 備考 |
|---|---|---|---|
| **L0 コア** | `core/` | `core/loop.py`, `data/models.py`, `execution/sm.py`(→ 安全層と汎用FSM基盤に分解), `execution/risk.py`, `execution/sim_broker.py`, `journal.py` | `TradingLoop` を執行モデル非依存に一般化 |
| **L1 インジケーター** | `indicators/` | `analysis/indicators.py`(SMA/RSI/ATR) | MACD等の受け皿。**汎用部品のみ** |
| **L2 手法** | `strategies/<name>/` | `strategy/provider.py` + `analysis/`の dow/elliot/fib/zigzag/support_resistance/confluence/micro/future_discretion | ダウFSM・エリオット等は**Narrow Focus 固有ロジック**として手法側へ |
| **L3 バックテスト** | `backtest/` | `backtest/engine.py`, `data/feed.py`, `data/exporter.py` | 期間スライス機能を追加 |
| **L4 フロントエンド** | `frontend/` or `ui/` | `dashboard/`, `backtest/report_html.py` | |
| **L5 運用** | `live/` | `execution/mt5_adapter.py`, `data/mt5_feed.py`, `ai/` | ブローカー実体・LLMゲート |

> `analysis/` の分割が肝。現状 `analysis/` には **L1相当の汎用部品(SMA/RSI/ATR)** と **L2相当の手法固有ロジック(ダウFSM/エリオット/コンフルエンス)** が同居している。ユーザーの③インジケーター層は「一般的なインジケーター」なので、前者をL1へ、後者は手法の一部としてL2へ移す。

## 4. 手法プラグイン契約 (本設計の核心)

手法を **Strategy(分析・シグナル生成)** と **ExecutionModel(執行ライフサイクル)** の2つの責務に分離する。1つの「手法」はこの2つの組で1パッケージを成す。

### 4.1 Strategy — 「いつ・どちらに・どんな根拠で建てたいか」

```python
class Strategy(Protocol):
    """確定足を受け、新規エントリー意図と手法固有の管理シグナルを出す。"""
    def on_candle(self, candle: Candle, ctx: MarketContext) -> StrategyOutput: ...
```

- `StrategyOutput` は「新規ポジションの **OpenIntent**」と「既存ポジションへの **管理シグナル**(任意の手法固有データ)」を持つ汎用コンテナ。
- `OpenIntent` は執行モデルに依存しない最小限の意図: 方向・参入種別(成行/指値)・参入価格(指値時)・初期SL・無効化価格・有効期限・**根拠特徴量(コンフルエンス必須)**・任意の手法固有ペイロード(Narrow Focus なら `w1_high` / `fib_target` 等をここに格納)。
- Narrow Focus の現 `InfersSignalProvider` は、ほぼそのまま `NarrowFocusStrategy` になる(出力を `OpenIntent` 語彙へ詰め替えるアダプタを噛ませる)。

### 4.2 ExecutionModel — 「建てた後どう管理し手仕舞うか」

```python
class ExecutionModel(Protocol):
    """1ポジションのライフサイクルを管理する状態機械。
    安全層(SL単調性・冪等ID)を必ず経由してブローカーを操作する。"""
    state: PosState                                   # 終端は CLOSED
    def place(self, intent: OpenIntent) -> None: ...  # 初期発注(成行/指値)
    def on_broker_event(self, ev: BrokerEvent) -> None: ...   # 約定/SLヒット
    def on_bar(self, candle: Candle, signal: object) -> None: ...
        # 毎確定足の自己管理。手法固有の執行ロジック(SL移動・部分利確・追撃・決済)を内包。
        # `signal` は同じ手法の Strategy が出した管理シグナル(型は手法が決める)。
```

- 現 `PositionFSM`(打診→ブレイク追撃→半利→ランナー)は **`NarrowFocusExecution`** という一実装になる。`on_wave1_break` / `on_half_tp_signal` / `on_runner_target` は `on_bar` の内部実装へ畳み込む。
- 新手法は別の `ExecutionModel` を書ける。例: `MarketTpSlExecution`(成行参入 + 固定TP/SL)、`TrailingStopExecution` 等。
- **TradingLoop は `ExecutionModel` の抽象メソッド(`place`/`on_broker_event`/`on_bar`/`state`)だけを呼ぶ。** 現状のように `on_wave1_break` 等の具象メソッドを直接呼ばない。これが一般化の本体。

### 4.3 共通安全層 — 全 ExecutionModel に強制される不変条件

執行が自由になっても、以下は **ExecutionModel の外側 / 基底で物理的に強制**し、迂回路を作らない:

| 不変条件 | 強制点 | CLAUDE.md |
|---|---|---|
| SL は利益方向にしか動かない | `SLMonotonicGuard`(全SL変更が必ず経由) | 第3条 |
| 二重発注・二重決済しない | 決定論的 `client_order_id` + `BrokerPort` 冪等 | 第10条 |
| 形成中バー・ティックで判定しない | `ExecutionModel.on_bar` は確定足のみ受領 | 第2条 |
| 発注はリスク拒否権を通過 | `TradingLoop` が `place` 前に `RiskManager.approve` | phase1 §11 |
| 全判断をジャーナルへ記録 | `TradingLoop` が意図・判定・発注・拒否を追記 | 第11条 |
| 防御はLLM非依存 | `ExecutionModel` は LLM を一切呼ばない(LLMは L2 Strategy のゲートのみ) | 第1条 |

> 設計の合言葉: **「Strategy と ExecutionModel は手法の自由、安全層は全手法の掟」**。

### 4.4 手法レジストリとマニフェスト

```python
@dataclass(frozen=True)
class StrategySpec:
    name: str                       # "depth50" 等の一意名
    build_strategy: Callable[..., Strategy]
    build_execution: Callable[..., ExecutionModel]
    default_params: Mapping[str, object]   # 既定パラメータ(現 ProviderConfig 相当)
    description: str
    methodology_doc: str | None     # 例: docs/entry-methodology.md
```

- 中央レジストリ(`strategies/registry.py`)に各手法を名前で登録。CLI は `--strategy depth50` で引く。
- `depth50` を最初の登録手法とする = 「`NarrowFocusStrategy` + `NarrowFocusExecution` + depth50パラメータ(macro_filter, depth_screen, depth_max=0.5 ...)」の名前付きプリセット。
- 現状の個別CLIフラグ(`--depth-max` 等)は手法パラメータの上書きとして残せる(`--strategy depth50 --set depth_max=0.4` のような形)。
- 新手法を足す = `strategies/<name>/` にディレクトリを作りレジストリに1行登録するだけ。手法は**増えることが前提**の構造。

## 5. バックテストの期間スライス (フル/ライト)

`BacktestEngine.run(candles, ...)` は `Iterable[Candle]` を受けるだけなので、**読込後のローソク足列を時刻でフィルタする薄い関数を1つ足すだけ**で実現できる(エンジン本体は不変)。

```python
def slice_candles(candles, *, start=None, end=None, last=None) -> list[Candle]:
    """確定足列を期間で切り出す。start/end は UTC。last は終端からの相対(例: '1y')。"""
```

CLI 案:
- `--period full`(=全期間, 既定) / `--period light`(=直近1年の糖衣)
- `--from 2024-01-01 --to 2025-01-01`(明示範囲)
- `--last 1y` / `--last 6m`(終端からの相対)

データは**5年Parquet 1ファイルを正**とし、期間はすべてそこからの切り出しで表現する(別ファイルを増やさない)。レポート出力ディレクトリ名に手法名と期間を含める(例: `reports/depth50_full/`, `reports/depth50_1y/`)。

## 6. 新ディレクトリ構成 (目標形)

```
src/infers/
  core/            # L0: loop.py, models.py, safety/(sl_guard, idempotency), risk.py,
                   #     sim_broker.py, broker_port.py, journal.py, execution_base.py
  indicators/      # L1: sma.py, rsi.py, atr.py, macd.py(新規) ...(汎用のみ)
  strategies/      # L2:
    registry.py
    narrow_focus/  #   depth50 等。strategy.py + execution.py + params.py +
                   #   analysis/(dow, elliot, fib, zigzag, sr, confluence, micro, future_discretion)
    <next>/        #   今後追加する手法
  backtest/        # L3: engine.py, slicing.py(新規), data_loader.py
  frontend/        # L4: report_html.py, dashboard/
  live/            # L5: mt5_adapter.py, mt5_feed.py, ai/
  main.py          # 単一エントリ(--strategy / --period を解釈しレジストリから組み立て)
```

## 7. 段階的移行計画

各段階の**完了ゲート = `pytest` 全件合格 + depth50 のバックテスト成績が完全一致**。再現コマンド
(reports/README.md記載の `--macro-wave2 --depth-screen --depth-max 0.50 --no-fib-score`)を実行し、
PF 1.487396352 / DD $325.77 / 624トレード / 純益$1,440.11 / 勝率53.37% と1ビットでもズレたら
原因を特定するまで次へ進まない(2026-06-19 段階2.0着手前に確定した再現値。コミット済み
`reports/rule_depth50/report_data.js` の数値($1,444.02)とは生成タイミングの差で微小に異なるが、
本書のゲートは「移行前後の再現値が一致すること」であり絶対値はこの再現値を基準とする)。
ゴールデンリプレイ([tests/test_journal.py](../tests/test_journal.py))も各段階で通す。

| 段階 | 内容 | リスク | 検証 | 状態 |
|---|---|---|---|---|
| **2.0** | ディレクトリ再編(import移動のみ・ロジック不変)。`indicators.py`(L1)・`zigzag/elliot/fibonacci/micro/future_discretion/confluence/provider`(L2 `strategies/narrow_focus/`)・`data/models.py`→`core/models.py` を移動 | 低(機械的) | テスト312件合格・depth50数値完全一致(2回検証) | **完了** (2026-06-19) |
| **2.1** | インジケーター層 `indicators/` をパッケージ化(`_common.py`/`sma.py`/`atr.py`/`rsi.py` に分割、`__init__.py`で再公開)。MACD など汎用部品の受け皿を用意 | 低 | テスト312件合格 | **完了** (2026-06-19) |
| **2.2** | 手法レジストリ導入(`strategies/registry.py`)。`narrow_focus`(素のProviderConfig既定値)と`depth50`(v1.0確定構成)を登録。`--strategy depth50` で選択可能。**既存のフラグ組み立て経路(`--depth-max`等)は`--strategy`未指定時1ビットも不変** | 低〜中 | テスト312件合格 + `--strategy depth50`のバックテストが従来フラグ経路と完全一致 | **完了** (2026-06-19) |
| **2.3a** | **ExecutionModel 抽象の抽出**。`core/execution.py` に `ExecutionModel` プロトコル + `BarOutcome` を定義。`PositionFSM` を `NarrowFocusExecution` 化(高レベルメソッド `place`/`on_broker_event`/`on_bar`/`close` 追加、低レベル遷移メソッドと旧名 `PositionFSM` 別名は温存)。`TradingLoop` は抽象4メソッドのみ呼び、`on_wave1_break` 等の具象呼び出しを撤去。`open_positions` のタプル形状 `(execution, plan)` は維持(mt5_adapter `reconcile_snapshot` と統合テストの互換性) | **高** | テスト324件合格(`runner_reversal_exit` ゲーティングを執行レベル+ループ委譲契約の2テストへ再編)+ depth50数値完全一致(PF1.487396352/624/DD$325.77/勝率53.37%/CACHE GO3124・NO_GO1034) | **完了** (2026-06-20) |
| **2.3b** | **L0↔L2 境界違反の解消**。`NarrowFocusExecution`/`PosState`/`FsmConfig` と `ProviderOutput`/`TradePlan` を L2(`strategies/narrow_focus/`)へ物理移設し、`core/loop.py` / `execution/sm.py` から `StructureEvent`/`SRZone`/`SwingPoint` の import を除去(L0は手法固有語彙を一切持たない)。旧パスは互換 re-export で維持 | 中〜高 | テスト全件合格 + depth50数値完全一致 | 未着手 |
| **2.4** | 期間スライス `--from`/`--to`/`--last`(`backtest/slicing.py`)。5年Parquet1ファイルを正としローソク足列を時刻でフィルタ(別ファイルを増やさない) | 低 | 単体テスト11件 + 全期間(フラグ無指定)がdepth50数値に完全一致 + `--last 1y`がCLI経由で実際に絞り込まれることを確認 | **完了** (2026-06-20) |
| **2.5** | **2つ目の手法**(別 ExecutionModel、例: 成行+固定TP/SL)を実際に追加し抽象を検証 | 中 | 新手法のバックテスト・depth50は不変 | 未着手 |

段階2.3 が山場。2.3a で `TradingLoop.on_candle` の `fsm.on_wave1_break(...)` 等の具象呼び出しを `execution.on_bar(candle, signal)` の汎用呼び出しへ畳み込み済み。半利・追撃・ランナーのトリガー条件は `NarrowFocusExecution.on_bar` 内部へ移植され、depth50 のビット完全一致でトレード単位の挙動同一性を確認した。`TradingLoop` は `ExecutionModel`(`place`/`on_broker_event`/`on_bar`/`close` + `volume_steps`/`closed`)のみに依存し、`signal` は `object` 型(手法固有ペイロード)に一般化済み。残るは 2.3b の物理移設(境界 import 除去)。

### 段階2.0で判明した既知の境界違反 (2.3bで解消予定)

`core/loop.py` と `execution/sm.py`(L0)が `analysis.dow.StructureEvent` と
`analysis.support_resistance.SRZone`(本来はNarrow Focus固有のL2型)を直接 import している。
これらは現時点で `analysis/` パッケージに残置し、`strategies/narrow_focus/` への完全移動を
見送った(L0がL2に依存する逆転を避けるため)。さらに `analysis/dow.py` は
`strategies.narrow_focus.zigzag.SwingPoint` 型を参照しており、これも同種の境界越えである。
**2.3a 完了時点の進捗**: `TradingLoop` のシグナル型は `object` へ一般化済みで、`core/loop.py`
が `StructureEvent`/`SRZone` を import するのは `ProviderOutput`/`TradePlan` の型注釈のためだけに
なった。2.3b でこれら2型を L2 へ物理移設すれば、`core/loop.py`・`execution/sm.py` から手法固有
語彙の import を完全に除去できる(`execution/sm.py` の FSM 本体も L2 `strategies/narrow_focus/`
へ移し、旧パスは互換 re-export で残す)。

## 8. 未解決事項 / レビューで詰めたい点

1. **管理シグナルの受け渡し**: Narrow Focus は半利トリガーに毎足の `rsi_value` / `sma90` / 重要SRゾーンを使う(現 `ProviderOutput`)。これを `StrategyOutput` の手法固有ペイロードとして `ExecutionModel.on_bar(candle, signal)` に渡す形でよいか(Strategy と ExecutionModel が同一手法内で型を共有する前提)。
2. **構造イベント(建値SL移動)の扱い**: ダウ `StructureEvent` 駆動の建値SL移動は Narrow Focus 固有。これも管理シグナル経由で `NarrowFocusExecution` に閉じ込める。
3. **複数手法の同時稼働**: 1プロセスで複数手法を並行運用するか(ポートフォリオ)、当面は1手法/1プロセスか。バックテストの横断比較は前者が無くても可能。
4. **レポート横断比較UI**: 手法×期間のマトリクスを L4 で一覧比較する画面(後続フェーズでよいか)。
5. **`--set key=val` での手法パラメータ上書き**の是非(実験の再現性 vs 利便性)。

---
**次アクション**: 本ドラフトをレビューいただき、特に §4(手法プラグイン契約)と §7(移行計画の段階分け)に合意が取れれば、段階2.0(機械的なディレクトリ再編)から着手する。

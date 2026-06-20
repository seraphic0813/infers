# INFERS — フェーズ1: システム基本設計図(アーキテクチャ)

**INFERS** (Intelligent Narrow Focus Elliot Realtime System)
対象手法: 「Narrow Focus トレード手法 総合マニュアル」
作成日: 2026-06-10 / ステータス: フェーズ1ドラフト

> **⚠ エントリー判定ロジックの正は [entry-methodology.md](../src/infers/strategies/narrow_focus/entry-methodology.md) に移管(2026-06-13。2026-06-20 docs/ → strategies/narrow_focus/ へ再配置)。**
> 本設計書の §3〜§5(マクロ/ミクロ分析・未来裁量によるエントリー組成)は、ダウ/エリオット/フィボ/
> レジサポの**機構の実装詳細**としては有効だが、**各要素の役割・ゲート構成**は entry-methodology.md を
> 優先する。特に「マクロ方向 = 200SMA位置(D1/1H)」「40%深さスクリーニング」「ニュース遮断」は
> 本設計書に未記載のため、entry-methodology.md を参照すること。防御・執行・出口(§6)は本設計書が正。

---

## 0. 本書の目的・スコープ・前提

本書は「Narrow Focus手法」を (a) Python自動取引Bot(データ取得+AI判断+執行)、(b) 長期間バックテスト、の2形態でシステム実装するための基本設計図である。フェーズ1の成果物は本設計書であり、コードは含まない(擬似コード・データ構造定義は含む)。

**前提条件**

| 項目 | 内容 |
|---|---|
| 実行環境 | Windows 11 (本リポジトリの開発環境)。MetaTrader5 Python統合はWindowsネイティブ対応のため好適 |
| 対象銘柄 | BTCUSD (Vantage Trading / MT4・MT5)、XAUUSD (Swift Trader / MT4・MT5) |
| 時間足 | マクロ: D1・W1 / ミクロ: M5・M15・H1 |
| 言語 | Python 3.12+ |
| AI | 決定論層(Python) + LLM判断層(Claude Haiku 4.5 / Claude Fable 5) |

**最重要設計原則(全章に優先)**

1. **防御はLLM非依存** — 損切り・建値SL・半分利確・キルスイッチは100%決定論的なPythonコードで完結させる。LLMは「新規エントリーの最終ゲート」と「曖昧性の裁定」のみを担い、LLM障害時のデフォルトは常に「何もしない(NO-TRADE)」。
2. **LLMはインジケーターを計算しない** — SMA/RSI/フィボ等の数値計算をLLMにやらせるのはコスト・精度の両面で不適。LLMには事前計算済みの数値特徴量だけを渡す。
3. **確定足主義(リペイント禁止)** — すべての判定は対象時間足の確定足クローズ時にのみ評価する。スイング・波カウントは「確定済み」フラグを持ち、確定前のデータで売買判断をしない。
4. **バックテストとライブの同一コードパス** — 戦略コアは純粋関数群とし、データフィードとブローカーだけをアダプタで差し替える。
5. **イベントソーシング** — すべての判断はその時点の特徴量スナップショットとともに追記専用ジャーナルへ記録し、後から完全再現できるようにする。

---

## 1. 全体アーキテクチャ

### 1.1 コンポーネント図

```
                         ┌──────────────────────────────────────────────┐
                         │                INFERS Core                   │
 ┌───────────────┐       │  ┌──────────────┐   ┌──────────────────┐    │
 │  Data Feed     │──────┼─▶│ Feature Engine│──▶│  Signal Engine   │    │
 │  (MT5 / WS)    │ OHLCV │  │ ・SMA/RSI/ATR │   │ ・ダウ状態機械    │    │
 └───────────────┘       │  │ ・リサンプル   │   │ ・エリオット計数   │    │
        │                │  └──────────────┘   │ ・フィボ目標       │    │
        ▼                │         │           │ ・レジサポ/グランビル│    │
 ┌───────────────┐       │         ▼           │ ・コンフルエンス統合 │    │
 │  Market Store  │       │  ┌──────────────┐  └────────┬─────────┘    │
 │ Parquet+DuckDB │       │  │ Foresight     │           │ Evidence/    │
 └───────────────┘       │  │ Engine        │───────────┤ Cluster      │
                         │  │ (未来裁量)     │           ▼              │
                         │  └──────────────┘   ┌──────────────────┐    │
                         │                     │   AI Gateway     │    │
                         │   L0: Python(常時)   │ L1: Haiku 4.5    │    │
                         │                     │ L2: Fable 5      │    │
                         │                     └────────┬─────────┘    │
                         │                              ▼              │
 ┌───────────────┐       │  ┌──────────────┐   ┌──────────────────┐    │
 │ Broker Adapter │◀──────┼──│ Risk Manager  │◀──│ Execution Engine │    │
 │ MT5(Vantage/   │ 注文   │  │ (独立拒否権)   │   │ (状態機械)        │    │
 │  Swift)        │       │  └──────────────┘   └──────────────────┘    │
 └───────────────┘       │            │                  │             │
                         │            ▼                  ▼             │
                         │  ┌─────────────────────────────────┐        │
                         │  │ Event Journal (追記専用) + 監視/通知 │        │
                         │  └─────────────────────────────────┘        │
                         └──────────────────────────────────────────────┘
```

データフロー: **確定足イベント → 特徴量更新 → シグナル評価 → (条件成立時のみ)AIゲート → リスク承認 → 執行**。
バックテスタは Data Feed と Broker Adapter を Historical/Sim 実装に差し替えて同じコアを駆動する。

### 1.2 ディレクトリ構成(フェーズ2実装時の骨格)

```
infers/
├── docs/
│   └── phase1-architecture.md      # 本書
├── src/infers/
│   ├── data/        # feed(mt5/ws), resampler, store(parquet+duckdb)
│   ├── indicators/  # sma, rsi(wilder状態保持), atr, swing(zigzag)
│   ├── macro/       # dow_state, elliott_counter, fib_projector
│   ├── micro/       # granville, sr_zones, confluence
│   ├── foresight/   # 未来裁量エンジン(sma_projection, rsi_inversion, grid)
│   ├── signal/      # evidence統合・スコアリング・シグナル生成
│   ├── ai/          # gateway, prompts/, schemas, budget, verdict_cache
│   ├── execution/   # position_fsm, orders, broker/(mt5_vantage, mt5_swift, sim)
│   ├── risk/        # 独立リスクマネージャー(拒否権)
│   ├── backtest/    # engine, intrabar_fill, batch_replay, metrics
│   └── journal/     # event sourcing, snapshot
├── tests/           # unit + property-based(hypothesis) + golden replay
└── config/          # symbols.yaml, thresholds.yaml, broker.yaml
```

### 1.3 技術スタック

| 層 | 技術 | 理由 |
|---|---|---|
| データ取得 | `MetaTrader5` (公式Pythonパッケージ) | Vantage/SwiftはMT4/5系。Windows環境でネイティブ動作し、ヒストリカル+リアルタイム+発注を一本化できる |
| 保存 | Parquet + DuckDB | 数年分のM1/M5を高速にスキャン。バックテストの列指向集計に強い |
| 数値計算 | numpy / polars | インジケーターはベクトル化。未来裁量グリッドも行列演算で軽量 |
| スキーマ | pydantic v2 / dataclasses(frozen) | 特徴量・判定結果・LLM入出力をすべて型で固定 |
| LLM | `anthropic` SDK (Messages API + Batches API) | structured outputs (`messages.parse`)、prompt caching、Batchを単一SDKで利用 |
| ログ | structlog (JSON Lines) | ジャーナルと監視の両立 |
| テスト | pytest + hypothesis | 状態機械の不変条件をプロパティベースで検証 |

---

## 2. データ層

```python
@dataclass(frozen=True)
class Candle:
    symbol: str
    tf: str                  # "M5" | "M15" | "H1" | "H4" | "D1" | "W1"
    open_time: datetime      # UTC固定。ブローカー時刻はアダプタで正規化
    o: Decimal; h: Decimal; l: Decimal; c: Decimal
    volume: int
    is_closed: bool          # 確定足のみ True で下流に流す
```

- **単一の真実は最小足(M1またはM5)**。上位足はリサンプラが生成し、ブローカー側の足とズレない境界規則(週足の開始曜日、日足の切替時刻=ブローカーサーバー時刻)を `broker.yaml` で銘柄ごとに定義する。
- 価格は内部的に**整数ティック**(`price_int = round(price / tick_size)`)で保持し、比較・等価判定の浮動小数誤差を排除する。表示・API入出力時のみDecimal/strへ変換。
- ギャップ・欠損の検出と再取得(バックフィル)はフィード層の責務。下流は「連続した確定足列」を前提にできる。

---

## 3. 要件1(前半): マクロ分析のシステム化

### 3.1 スイング検出(ZigZag + 確定遅延)

ダウ理論・エリオット・レジサポの全てが「スイングポイント列」を入力とするため、これが分析層の最重要基盤となる。

- **反転閾値**: `θ_rev = max(k_atr × ATR(14), k_pct × price)`(銘柄・TF別に設定。例: H1で k_atr=1.5)。
- **確定規則**: 直近極値から逆方向に `θ_rev` 以上動いた確定足クローズ時点で、その極値をスイングとして「確定」する。**確定までの遅延は仕様であり、ノイズ除去機能そのもの**(7章の建値SL移動でこの遅延を意図的に利用する)。

```python
@dataclass(frozen=True)
class SwingPoint:
    kind: Literal["HIGH", "LOW"]
    bar_time: datetime
    price_int: int
    tf: str
    confirmed_at: datetime    # リペイント防止: この時刻以降のみ判断材料にできる
```

### 3.2 ダウ理論 状態機械

確定スイング列から HH/HL/LH/LL イベントを生成し、有限状態機械でトレンドを管理する。マニュアル2.1の「高値更新失敗 + 安値切り下げで転換確定」を状態遷移として厳密に表現する。

| 現状態 | イベント | 次状態 | 意味 |
|---|---|---|---|
| `UP` | LH (高値切り下げ=更新失敗) | `UP_SUSPECT` | 転換警戒 |
| `UP_SUSPECT` | LL (安値切り下げ) | `DOWN` | **転換確定** |
| `UP_SUSPECT` | HH (高値更新) | `UP` | 警戒解除 |
| `DOWN` | HL | `DOWN_SUSPECT` | (対称) |
| `DOWN_SUSPECT` | HH | `UP` | 転換確定 |
| `DOWN_SUSPECT` | LL | `DOWN` | 警戒解除 |
| `UNDEFINED` | HH+HL成立 / LH+LL成立 | `UP` / `DOWN` | 初期判定 |

```python
class TrendState(Enum):
    UP = auto(); UP_SUSPECT = auto(); DOWN = auto(); DOWN_SUSPECT = auto(); UNDEFINED = auto()

@dataclass
class DowState:
    tf: str
    state: TrendState
    last_high: SwingPoint | None
    last_low: SwingPoint | None
    events: list[StructureEvent]     # HH/HL/LH/LL の履歴(ジャーナルへ)
```

状態機械はTFごとに独立に1インスタンス(D1, H1, M15…)。**「安値切り上げ確定」「高値更新確定」イベント**はこの機械が発行し、エントリー根拠(3.6)と建値SL移動(7.3)の両方が購読する。

### 3.3 エリオット波動カウンター(3原則 = 無効化価格への変換)

**設計の核心: 3つの絶対原則は「将来この価格を割ったらカウント無効」という無効化価格(invalidation price)に変換できる。** これにより毎ティックの再検証が O(1) になり、同じ価格がそのまま損切り・シナリオ破棄ラインとして執行層へ流用できる。

| 原則 | プログラム上の制約 | 導出される無効化価格(上昇波の例) |
|---|---|---|
| ① 第3波は推進波中最短にならない | `len(W3) >= min(len(W1), len(W5))` を波確定時に検証 | 第5波進行中: `W5長 > W3長` となった時点で無効 |
| ② 第2波は第1波の始点を割らない | `W2.low > W1.start` | **`W1.start.price` を下抜けたら即無効** |
| ③ 第4波は第1波の高値を割らない | `W4.low > W1.end` | **`W1.end.price` を下抜けたら即無効** |

カウントは本質的に多義的なので、**単一の正解を持たず候補集合を保持**する:

```python
@dataclass
class WaveCount:
    degree: str                       # 計数対象TF ("D1"など)
    pivots: list[SwingPoint]          # 波0..5 (+A,B,C) の境界スイング
    current_wave: int                 # 進行中の波番号 (1..5, -1=修正ABC)
    invalidation_price: int           # 原則②③から自動導出。割れたら valid=False
    score: float                      # フィボ適合度(W2押し38.2-78.6%等)+時間比率の複合
    parent: "WaveCount | None"        # フラクタル: D1の第3波 ⊃ H1の推進5波

@dataclass
class ElliottView:
    candidates: list[WaveCount]       # スコア降順 top-N (N=3程度)
    ambiguity: float                  # 1位と2位のスコア差(小=曖昧)。AIエスカレーション指標
```

- 候補列挙は「直近の確定スイング最大12点」に対する波境界の割当探索。3原則違反は即枝刈り、残候補をフィボ比率・時間比率でスコアリング。
- **フラクタル接続**: 上位足カウントの「現在波」区間内で下位足カウントを走らせ、`parent` で連結する(例: D1第2波の終点探索中に、H1で下降ABC完了+上昇1波を検出)。
- `ambiguity` が閾値以下(=候補が拮抗)かつポジション判断に直結する局面が、L2(Fable 5)へ裁定を委ねる代表ケース(8章)。

### 3.4 フィボナッチ目標値

```python
@dataclass(frozen=True)
class FibTarget:
    wave_count_id: str
    target_wave: int                  # 3 または 5
    base_len: int                     # 第1波の値幅(ティック)
    anchor: SwingPoint                # 第2波終点(→W3目標) / 第4波終点(→W5目標)
    levels: dict[str, int]            # {"100.0": p, "161.8": p, "261.8": p}
```

マニュアル2.3に忠実: `W3目標 = W2終点 + W1長 × {1.0, 1.618, 2.618}`、`W5目標 = W4終点 + W1長 × {…}`。各レベルは Evidence(3.6) と利確目標(7.4)の双方に供給される。

---

## 4. 要件1(後半): ミクロ分析のシステム化

### 4.1 SMA(90/200) と グランビルの法則の数値化

「移動平均まで下落して反発」のような裁量表現は、**正規化乖離 d と SMA傾きで形式化**する:

- `d(t) = (close(t) − SMA(t)) / ATR(t)` … ATR単位の乖離
- `slope(t) = (SMA(t) − SMA(t−n)) / (n × ATR(t))` … 正規化傾き(n=5など)

| サイン | 形式条件(買い側。売りは対称) |
|---|---|
| 買②(押し目からの再上昇) | 過去m本内に `d>θ_touch` から `|d|≤θ_touch` へ接近 → 陽線反転バー(`close>open` かつ `close>前バーhigh`) かつ `slope≥0` |
| 買③(SMAサポート反発) | バーの安値が `|d_low|≤θ_touch`(またはわずかに割る `d_low≥−θ_pierce`) かつ 終値がSMA上 かつ `slope≥0` |
| 買④(大幅下方乖離からの戻り) | `d≤−θ_far`(例: −3.0) かつ 反転トリガーバー成立 |

パラメータ既定値(要バックテスト調整): `θ_touch=0.3, θ_pierce=0.8, θ_far=3.0`。各成立は Evidence として発行。

### 4.2 RSI(14) — Wilder方式・状態保持

```python
@dataclass
class RsiState:
    period: int            # 14
    avg_gain: float        # Wilder平滑の内部状態
    avg_loss: float
    value: float           # 現在RSI
```

- 30以下到達/70以上到達のクロスで Evidence 発行(買い根拠/利確根拠の両方にタグ付け)。
- **内部状態 `avg_gain / avg_loss` を保持・公開する点が重要**。未来裁量エンジン(5章)のRSI逆算はこの状態から出発する。

### 4.3 レジサポライン(水平ゾーン)

ラインは「点」ではなく**幅を持つゾーン**として扱う(約定の現実とコンフルエンス判定の安定性のため):

```python
@dataclass
class SRZone:
    low_int: int; high_int: int       # 幅 = ε_zone = α_zone × ATR (例 α=0.5)
    touches: int                      # タッチ回数(強度)
    last_role: Literal["SUPPORT", "RESISTANCE"]
    flipped: bool                     # ブレイク済み=役割転換(マニュアル3.3)
    strength: float                   # Σ(タッチ重み × 経過時間減衰)
```

- 生成: 直近W本(TF別)の確定スイングを価格でクラスタリング(中心距離 < ε_zone で併合)。
- 役割転換: 終値がゾーンを `margin = β×ATR` 超えて抜けたら `flipped=True`・role反転。転換後の**リテスト反発**は高重み Evidence。

### 4.4 コンフルエンス統合(根拠≥2の機械化)

マニュアル3.4「必ず根拠が2つ以上重なるポイント」を、**Evidence(根拠オブジェクト)のゾーン重なり判定**として実装する。

```python
@dataclass(frozen=True)
class Evidence:
    family: Literal["ELLIOTT", "DOW", "GRANVILLE", "RSI", "SR", "FIB"]
    source: str               # 例 "GRANVILLE_BUY3_SMA90_H1"
    direction: int            # +1 買い / -1 売り
    tf: str
    zone: tuple[int, int]     # 有効価格帯(整数ティック)
    weight: float             # 基礎重み
    valid_until: datetime | None

@dataclass
class ConfluenceCluster:
    zone: tuple[int, int]             # Evidenceゾーンの交差区間
    direction: int
    evidences: list[Evidence]
    distinct_families: int            # ★ ≥2 が必須条件
    score: float                      # Σ weight × τ(tf)
```

- **TF係数 τ**: M5=1.0, M15=1.2, H1=1.5, H4=2.0, D1=3.0(上位足の根拠ほど重い)。
- **family重複の排除**: M5のRSI30とM15のRSI30は「同一family」として1根拠と数える(独立な根拠2つ、を厳密化)。
- 方向の不一致(買い根拠と売り根拠の混在ゾーン)はクラスタ不成立。
- `score ≥ S1` で L1 へ、`score ≥ S2 (かつL1承認)` で L2 へ(8章)。打診エントリー候補は **常にクラスタ単位**で生成され、`invalidation_price`(エリオット由来)と初期SL候補(直近スイング)を同梱する。

---

## 5. 要件2: 「未来裁量」のアルゴリズム化 (Foresight Engine)

### 5.1 問題の定式化

> 現在時刻 t0・現在値 c0 から、「k本後に価格が P に到達した」と仮定したとき、その到達時点で複数インジケーターが同時に合流する (P, k) の集合を求める。

つまり未来裁量とは **(時間 × 価格) 平面上の逆問題**である:

```
S = { (P, k) :  |RSI(t0+k | path→P) − 30| ≤ ε_rsi
             ∧ |P − SMA90(t0+k | path→P)| ≤ δ_sma
             ∧ P ∈ FibTarget帯  ∧ P ∈ SRZone帯  … のうち2条件以上 }
```

鍵となる洞察は2つ:
1. **SMAの未来値は「未来の終値の合計」だけに依存**し、パス形状にほぼ依らない → 線形パス仮定で**解析解**が出る。
2. **RSIはパス依存**だが、Wilder状態 (avg_gain, avg_loss) からの前進シミュレーションが超軽量 → 代表パス族で**バンド(区間)**として評価できる。

### 5.2 SMA前方投影 — 「伸びてくるSMAとの接触点」の閉形式解

期間 m のSMAについて、k本後(k < m)の値は:

```
SMA(t0+k) = ( S_known(k) + Σ_{i=1..k} c_i ) / m
   S_known(k) = 直近 (m−k) 本の既知終値の和(完全に既知)
```

c0 から P への**線形パス** `c_i = c0 + (P−c0)·i/k` を仮定すると `Σc_i = k·c0 + (P−c0)(k+1)/2` となり、**接触条件 P = SMA(t0+k) は P について1次方程式**になる:

```
P*(k) = ( S_known(k) + c0·(k−1)/2 ) / ( m − (k+1)/2 )
```

- k = 1..K を掃引すると **「SMAタッチ曲線」P*(k)** が得られる。これがマニュアル4の「時間経過によって伸びてきた日足90SMAにピタリと接触する価格」の数学的実体である。
- 検算: k=1 のとき `P* = S_{m−1}/(m−1)`(既知の直近 m−1 本の和から一意に決まる)。
- **日足SMAを日中足から扱う場合**: d日先の日足SMA90は、未知項の重みが d/90 しかないため、価格パスにほぼ依存しない狭い回廊(corridor)として計算できる。`[P_min, P_max]` のパス包絡を入れて回廊幅を持たせる。

### 5.3 RSI逆算

**(a) 1本先の閉形式解** — Wilder更新 `g' = 13g/14, l' = (13l+Δ)/14`(下落幅Δ)に対し、目標RSI=R到達条件 `l' = g'(100−R)/R` を解くと:

```
Δ_required = 13 × ( g·(100−R)/R − l )        # R=30 なら 13(7g/3 − l)
P_RSI30(1本) = c0 − Δ_required               # Δ≤0 なら既に条件圏内
```

**(b) k本先(パス依存)** — 状態 (g, l) から k回のWilder更新を前進計算するだけ(k≤100でもマイクロ秒オーダー)。パス形状で結果が変わるため、**代表パス族**で評価しバンドを得る:

| パス | 形状 | 到達時RSIへの影響 |
|---|---|---|
| 線形(等分下落) | 各バー均等に下落 | 中央推定値 |
| 後傾(back-loaded) | 直近バーに下落集中 | **最小RSI**(バンド下限) |
| 前傾(front-loaded) | 序盤に下落集中→平滑で減衰 | 高め |
| 戻り挟み(retrace ρ=0.3) | 下落途中に反発を挟む | **最大RSI**(バンド上限) |

判定は `RSI_band(P,k) = [rsi_lo, rsi_hi]` に対し、`rsi_hi ≤ 30` なら「ほぼ確実に到達」、`rsi_lo ≤ 30 < rsi_hi` なら「パス次第」としてスコアを傾斜配分する。

### 5.4 未来コンフルエンスマップ(グリッド探索)

```
for k in 1..K:                        # 例 H1ならK=72 (3日先まで)
    for P in price_grid:              # c0±5ATR を 0.1ATR 刻み
        f = FeatureVector(
            rsi_band      = rsi_forward(g, l, c0→P, k, path_family),
            sma90_h1_dist = |P − SMA90_H1_proj(k, P)|/ATR,
            sma90_d1_dist = |P − SMA90_D1_corridor(k)|/ATR,
            sma200_dist   = …,
            fib_hit       = P ∈ FibTarget帯(161.8/261.8),
            sr_hit        = P ∈ SRZone帯,
            elliott_ok    = P が現行WaveCountの想定範囲内(無効化価格に未抵触),
        )
        cell_score = confluence_score(f)       # 4.4と同じfamily≥2規則を適用
```

K×グリッド ≈ 72×100 セル程度。numpyでベクトル化すれば1銘柄あたり数ミリ秒で再計算可能(毎確定足で全再計算する設計で問題ない)。

### 5.5 指値注文の生成と失効管理

スコア上位セルの連結領域から候補を生成する:

```python
@dataclass
class FutureConfluence:
    symbol: str
    direction: int
    limit_price: int                  # 指値価格(セル重心)
    eta_window: tuple[int, int]       # 有効な k 範囲 [k_min, k_max]
    expected: dict                    # 到達時の予測値 {rsi_band, sma_dist, …} ← L2へ渡す特徴量
    score: float
    expiry: datetime                  # ★ k_max 経過で失効(SMAが動くため必須)
    invalidation_price: int           # エリオット無効化と同期。抵触で即キャンセル
```

- **コンフルエンス点は(価格×時間)の点であり、純粋な価格の点ではない**。SMAは時間とともに動くため、指値には必ず `expiry` を付け、毎確定足で `P*(k)` を再計算し、許容ドリフトを超えたら注文を修正(amend)または取消す。
- 約定した場合は7章の状態機械が `PROBE_FILLED` から引き継ぐ。マニュアル4の「到達・反発は確率イーブン」という前提どおり、**この指値は常に打診サイズ**であり、SLは注文と同時にセットする(7.2)。

### 5.6 フェーズ2拡張(任意)

ブートストラップした過去リターン系列を P 到達条件で条件付け(ブラウン橋的)したモンテカルロで「到達時にRSI≤30となる確率」を推定する拡張が可能。フェーズ1では決定論的なパス族バンドで十分とする。

---

## 6. 要件3: 資金管理・防御策の例外処理設計

### 6.1 ポジション・ライフサイクル状態機械

ポジション管理は**明示的な有限状態機械**とし、状態はEnum一本で表す(bool フラグの組合せ管理は禁止 — 不正状態が組合せ爆発するため)。

```
IDLE ──(クラスタ成立+AI承認+リスク承認)──▶ PROBE_ORDER_PLACED ──(約定)──▶ PROBE_FILLED
                                                  │(expiry/無効化)              │
                                                  ▼                            │(W1高値ブレイク確定)
                                                CANCELLED                       ▼
        ┌──────────────────────────────────────────────────────────  ADD_ORDER_PLACED
        ▼                                                                      │(約定)
   (SL執行) ◀──────────────────────────────────────────────────────────────────┤
        ▲                                                                      ▼
        │                                            ┌──(ダウ構造更新確定)── ADD_FILLED
     CLOSED ◀──(残玉決済: フィボ目標/転換シグナル)── RUNNER ◀──(半分利確済)── SL_AT_BE
```

| 状態 | 不変条件(違反したら即アラート+安全側へ) |
|---|---|
| `PROBE_FILLED` | SLが必ず存在する(SL設定失敗時は成行クローズが最優先) |
| `ADD_FILLED` | 合計ロット ≤ 設定上限。追撃玉にも独立した初期SL |
| `SL_AT_BE` | 買いのSL ≥ 建値。**SLは利益方向にしか動かない(単調性)** |
| `RUNNER` | 半分利確は厳密に1回だけ実行済み |

### 6.2 トリガー定義の厳密化 — 「第1波高値超え」(追撃)

裁量の「超えたら」を、バグなく執行するために以下で固定する:

```
追撃トリガー = decision_tf(例: H1)の確定足終値 > W1_high + buffer
buffer = max( α_atr × ATR,  n_ticks × tick_size,  spread_now × m_spread )
```

- **ティックのヒゲ抜けでは発火しない**(確定足終値で判定)。これはマニュアル5.1「第3波が確定したと判断できたタイミング」の機械化であり、若干の遅れと引き換えにダマシを排除する設計判断。
- `W1_high` は**追撃判断時点で有効な WaveCount 候補の第1波高値**。候補が複数ある場合は保守側(最も高い W1_high)を採用。
- 発火後の追撃注文は成行(またはストップ注文を事前設置)。どちらにするかは `config` で切替可能とし、バックテストで比較する。

### 6.3 建値SL移動 — ダウ理論ベースの「波の更新確定」駆動

マニュアル5.2の注意点(含み益だけで建値SLにするとノイズに狩られる)を、**イベント駆動**として実装する:

```
建値SL移動の条件(買いの場合) — すべて成立で実行:
  1. DowState(decision_tf) が「安値切り上げ確定」イベントを発行した
     (= 新しいスイングローが確定し、その価格 > 前回スイングロー)
  2. その確定スイングロー価格 > エントリー価格 + min_be_distance
     (min_be_distance = max(spread×κ, ブローカー最小ストップ距離, ε_atr×ATR))
  3. 現在状態 ∈ {PROBE_FILLED, ADD_FILLED}
```

- 条件1のスイング「確定」には3.1の反転閾値による遅延が内在する。**この遅延こそがノイズ耐性**であり、「含み益が出た瞬間に建値へ」という早計(マニュアルが明示的に禁じる行為)をコード構造として不可能にする。
- 含み益額・pips を建値移動のトリガーに**使ってはならない**(実装者への明示的禁止事項としてテストで担保)。

### 6.4 半分利確の厳密一回実行

```
トリガー: RSI(decision_tf) が 30/70 に到達 or 重要SRZone帯に到達(確定足 or 指値)
実行:    half_volume = round_to_lot_step(initial_total_volume / 2)
         runner_volume = initial_total_volume − half_volume   # 端数はrunner側へ
```

- `half_volume` と `runner_volume` は**エントリー完了時点で計算して固定**し、ジャーナルに記録(後から「半分」の定義が変わらないように)。
- 実行は状態遷移 `SL_AT_BE → RUNNER` と不可分(1トランザクション)。送信失敗時はリトライするが、**冪等キーにより二重決済を構造的に防ぐ**(6.5)。
- 残玉の利確目標 = `FibTarget.levels["161.8"]`(第3波運用時)。到達 or ダウ転換シグナルでクローズ。

### 6.5 コード設計上の注意点(チェックリスト)

| # | 注意点 | 実装方針 |
|---|---|---|
| 1 | **冪等性** | 全注文操作に決定論的 `client_order_id = hash(position_id, transition, seq)` を付与。再送してもブローカー側で重複しない |
| 2 | **リコンサイル** | 起動時・再接続時・定期(30s)に、ローカル状態機械とブローカーの実ポジション/注文を突合。乖離は即アラート+取引停止 |
| 3 | **部分約定** | 打診・追撃は別注文として管理。部分約定は累積数量で状態を更新し、SL数量を常に保有数量へ同期 |
| 4 | **SL設定失敗** | エントリー約定後T秒以内にSLが確認できなければ成行クローズ(防御最優先のフォールバック) |
| 5 | **数値表現** | 価格は整数ティック、数量はロットステップの整数倍で保持。float比較禁止 |
| 6 | **ブローカー制約** | 最小ストップ距離・フリーズレベル・最小/最大ロットをアダプタ層で事前検証してから送信 |
| 7 | **プロパティテスト** | hypothesisで不変条件を網羅: 「SLは利益方向にのみ移動」「半分利確は高々1回」「全約定済み数量 ≤ 発注数量」「無効化価格抵触後に新規注文が出ない」 |
| 8 | **イベントソーシング** | 状態遷移は `(遷移名, 入力イベント, 特徴量スナップショット, 結果)` を追記専用ジャーナルに記録。リプレイで任意時点の判断を完全再現 |
| 9 | **時刻** | 判定はすべて「確定足のクローズ時刻」基準。ローカル時計は使わずフィードのサーバー時刻に従う |
| 10 | **二重起動防止** | プロセスロック+ブローカー側マジックナンバーで、多重Botの相互干渉を遮断 |

### 6.6 独立リスクマネージャー(拒否権レイヤー)

戦略・AIの判断とは**独立したプロセス/モジュール**として、全注文を最終検査する:

- 日次最大損失(口座残高比%)到達 → 当日新規禁止+キルスイッチ
- 最大同時エクスポージャー、銘柄あたり最大ロット
- スプレッド異常(平常時のx倍超)時のエントリー拒否(指標発表スパイク対策)
- 接続断・リコンサイル不一致時の新規停止
- 設定はホットリロード不可(再起動必須)とし、稼働中の誤変更を防ぐ

---

## 7. 要件4: ハイブリッドAI判断層(APIコストと精度の最適化)

### 7.1 3層構造

> 補足: 「ベースのインジケーター判定をGemini 1.5 Pro等で」という案について — インジケーター計算・閾値判定そのものは決定論計算であり、**どのLLMにもやらせるべきではない**(L0で無料・正確・マイクロ秒)。LLMが価値を持つのは「波カウントの妥当性」「未来コンフルエンスの文脈裁定」のような半定性判断のみ。なお Gemini 1.5 Pro は旧世代(提供終了済み)のため、他社モデルを使う場合もその時点の現行軽量モデルをアダプタ経由で差し替える設計とする。推奨は同一SDKでcaching/Batch/structured outputsを共有できる **Haiku 4.5**。

| 層 | 担当 | 実体 | 頻度 | コスト |
|---|---|---|---|---|
| **L0** | 全インジケーター計算、ダウ状態機械、エリオット3原則検証、フィボ、レジサポ、コンフルエンススコア、未来コンフルエンスマップ、**全防御ロジック** | Python (numpy/polars) | 毎確定足 (M5基準: 288回/日/銘柄) | ゼロ |
| **L1** | 一次トリアージ: 波カウント候補の整合性チェック、偽陽性フィルタ、状況の構造化要約 | `claude-haiku-4-5` ($1/$5 per MTok) | クラスタ score ≥ S1 時のみ (目安 5〜15回/日) | 微小 |
| **L2** | 最終ジャッジ: エントリーgo/no-go、エリオット曖昧性の裁定、未来コンフルエンス候補のシナリオ推論、トレードプラン合成(無効化条件の言語化) | `claude-fable-5` ($10/$50 per MTok, adaptive thinking + effort high) | (score ≥ S2 ∧ L1承認) ∨ FutureConfluence score ≥ F2 (目安 0〜3回/日) | 限定 |

**エスカレーションは決定論的なポリシー関数**(ジャーナルに記録され、バックテストで再現可能):

```python
def escalation(cluster, elliott_view, budget) -> Tier:
    if cluster.distinct_families < 2:           return Tier.NONE      # マニュアル3.4の絶対条件
    if cluster.score < S1:                      return Tier.NONE
    if budget.l2_exhausted():                   return Tier.NONE      # 予算切れ=NO-TRADE(防御的デフォルト)
    if cluster.score >= S2 or elliott_view.ambiguity < A_GRAY:
        return Tier.L2_AFTER_L1                 # L1で却下されればL2は呼ばない
    return Tier.L1_ONLY
```

- 同一状況での再問合せ防止: `feature_hash = sha256(正規化特徴量)` + クールダウン(同ハッシュはTTL内再問合せ禁止、判定はキャッシュから返す)。
- **L2の判定が得られない場合(API障害・タイムアウト・予算超過)のデフォルトは常に「エントリーしない」**。ポジション管理(SL/利確)はL0のみで完結しているため、LLM全停止でも防御は機能し続ける。

### 7.2 API設計(リクエスト/レスポンス契約)

LLMへの入力は生ローソク足ではなく**事前消化済みの数値特徴量のみ**(トークン削減+幻覚抑制):

```python
class JudgementRequest(BaseModel):
    kind: Literal["ENTRY_GATE", "WAVE_DISAMBIGUATION", "FUTURE_CONFLUENCE_REVIEW"]
    symbol: str
    features: dict          # DowState各TF, ElliottView候補(各波の価格/比率/無効化), 
                            # cluster内Evidence一覧, RSI/SMA乖離, FutureConfluence.expected 等
    constraints: dict       # 口座リスク上限、現在エクスポージャー

class Verdict(BaseModel):   # structured outputs (client.messages.parse) で強制
    decision: Literal["GO", "NO_GO", "WAIT"]
    confidence: float       # 0..1
    selected_wave_count: int | None     # 候補indexの裁定
    invalidation_price: float | None    # 「このシナリオが死ぬ価格」の言語化→数値化
    reasons: list[str]      # ジャーナル用(3項目以内)
```

呼び出し規約(Fable 5):
- `thinking={"type": "adaptive"}` + `output_config={"effort": "high"}`(Fable 5は明示 `disabled` 不可・samplingパラメータ不可。プリフィル不可のため**structured outputsで形式を強制**)
- L1(Haiku 4.5)は `effort` 非対応のため通常呼び出し+`messages.parse`。
- SDKの自動リトライ(429/5xx)+タイムアウト30s。失敗時は `NO_GO` 扱い。

### 7.3 Prompt caching 設計

- **システムプロンプト = 手法マニュアル全文 + 判定ルール + few-shot例(約8〜10kトークン)を凍結**し、`cache_control: {"type": "ephemeral"}` を末尾ブロックに付与。タイムスタンプ・乱数等の揮発要素はシステムプロンプトに一切入れない(キャッシュ全壊のため)。
- 可変部(features JSON)は messages 側に置き、キー順を `sort_keys=True` で決定論化。
- 最小キャッシュ対象: Fable 5 = 2048tok、Haiku 4.5 = 4096tok — 上記プロンプトサイズなら両者ともキャッシュ可能。
- キャッシュはモデル単位(L1/L2間で共有されない)。L1は呼び出し頻度が高く5分TTLで自然にヒット。L2は呼び出しが疎(数時間間隔)なのでキャッシュミス前提のコスト見積りとする(7.5)。

### 7.4 バックテストでのLLM利用 — 2パス + Batch API(50%オフ)

数年分のM5を回す際にLLMを同期呼び出ししてはならない。**決定論パスとLLM裁定を分離した2パス構成**にする:

```
Pass 1: L0のみで全期間を高速スイープ
        → エスカレーション条件を満たした「裁定イベント」を全件収集 (feature_hash付き)
Pass 2: feature_hashで重複排除 → Batches API (50%オフ・24h以内完了) に一括投入
        → Verdictを verdict_cache (SQLite: key=(model, prompt_version, feature_hash)) へ永続化
Pass 3: verdict_cacheを参照しながら最終リプレイ → 損益計算
```

- パラメータを変えた再実行は、ハッシュが一致する限り**キャッシュヒットで無料**。
- `prompt_version` をキーに含めることで、プロンプト改訂時に古い判定を誤って再利用しない。

### 7.5 コスト試算(2銘柄・2026年6月時点の料金)

前提: L1=15回/日(入力4k tok中3kキャッシュ済/出力0.3k)、L2=3回/日(入力10k/出力3k thinking込み、キャッシュミス前提)。

| 項目 | 計算 | 月額目安 |
|---|---|---|
| L0 (Python) | — | $0 |
| L1 Haiku 4.5 | ≈$0.003/回 × 15回 × 30日 | **≈$1.4** |
| L2 Fable 5 | ≈$0.18〜0.28/回 × 3回 × 30日 | **≈$16〜25** |
| **ライブ運用 合計** | | **月 $20〜30 程度** |
| バックテスト(5年・裁定イベント1,500件と仮定) | L1 Batch ≈$4 + L2 Batch(300件) ≈$38 | **1回 $40〜45**(再実行はキャッシュでほぼ$0) |

参考: もし全確定足(288×2銘柄×30日≈17,000回/月)をFable 5に直接判定させた場合、入力10k tokなら入力だけで月$1,700超。**L0/L1での絞り込みがコストを約2桁圧縮する**のがこのアーキテクチャの要点。

### 7.6 フェイルセーフまとめ

- LLM層全停止 → 新規エントリーのみ停止、既存ポジションの防御はL0で継続。
- L2予算(回数/日・$/月)超過 → NO-TRADE。予算カウンタはジャーナルに永続化し再起動でリセットされない。
- Verdictのスキーマ検証失敗(parse例外) → 1回だけ再試行、再失敗で NO_GO。

---

## 8. バックテスト設計(ライブ同等性)

- **イベント駆動エンジン**: 確定足イベントをヒストリカルフィードが順次発行し、ライブと同一の Strategy コアを駆動。`SimBroker` がスプレッド(ブローカー別実測分布)・スリッページ・最小ストップ距離を再現。
- **イントラバー約定**: SL/TP/指値の同一バー内到達順序はM1(可能ならティック)データで解決。M1が無い区間は保守側(SL先行ヒット)を採用。
- **ウォークフォワード**: パラメータ(θ_touch, S1, S2, buffer係数等)はin-sampleで探索し、out-of-sampleで検証。期間ローテーションで過剰最適化を検出。
- **指標**: PF、最大DD、勝率、平均R、R分布、建値SL退出率、半分利確後のrunner期待値 — 手法の主張(「負けないトレード」)を建値SL退出率とR分布で定量検証する。
- **ゴールデンリプレイテスト**: ジャーナルに記録した過去の実判断列をテストフィクスチャ化し、コード変更後も同一入力→同一判断であることをCIで担保。

---

## 9. 非機能要件

| 項目 | 方針 |
|---|---|
| 稼働形態 | Windowsサービス化(NSSM)またはタスクスケジューラ常駐。watchdogプロセスがハートビート監視 |
| 監視・通知 | 構造化ログ(JSONL)+重要イベント(エントリー/SL/リコンサイル不一致/キルスイッチ)をWebhook通知(Discord/Telegram等) |
| 設定 | pydantic-settings + YAML(銘柄・閾値・ブローカー)。APIキーは環境変数のみ(コード・ログに残さない) |
| 再接続 | MT5切断時は指数バックオフで再接続→リコンサイル→再開。再接続不能T分でアラート |
| タイムゾーン | 内部UTC統一。ブローカーサーバー時刻オフセットはアダプタで吸収 |
| セキュリティ | 口座資格情報はWindows資格情報マネージャー/環境変数。ジャーナルに残高比率以外の口座情報を書かない |

---

## 10. フェーズ2以降の実装ロードマップ

| フェーズ | 内容 | 完了条件 | 状況(2026-06-15) |
|---|---|---|---|
| 2 | データ層+インジケーター+スイング検出 | 5年分M5の取込みとZigZag確定ロジックの単体テスト合格 | ✅ 完了 |
| 3 | ダウ状態機械+エリオット計数+フィボ | 既知チャートでの波カウント候補がマニュアルの解釈と一致 | ✅ 完了 |
| 4 | コンフルエンス統合+バックテストコア(L0のみ) | 全期間スイープが完走しメトリクスが出る | ✅ 完了(`rule_depth50`で624トレード) |
| 5 | 未来裁量エンジン | SMAタッチ曲線・RSIバンドの数値検証(手計算と一致) | ✅ 完了 |
| 6 | 執行状態機械+Sim/リスクマネージャー | プロパティテスト全合格+ゴールデンリプレイ | ✅ 完了(pytest 265件・ゴールデンリプレイ `tests/test_journal.py`) |
| 7 | AI Gateway(L1/L2)+Batchリプレイ | 2パスバックテストでVerdictキャッシュが機能 | ✅ 完了(`AnthropicLlmClient`/Batches API/verdict_cache実装済み) |
| 8 | MT5ライブ接続(デモ口座)+監視 | デモ環境で30日無人稼働、リコンサイル不一致ゼロ | ⚠ 無人稼働の前提実装(ジャーナル永続化・フィード再接続/欠損バックフィル・リコンサイル継続実行)はすべて完了。あとはデモ口座での実走検証のみ(下記の並行課題も参照) |

### v1.0確定とフェーズ8(デモ運用)の残課題(2026-06-15)

`rule_depth50`(`depth_max=0.50`)を**v1.0確定ベースライン**とする
(詳細: `reports/README.md`、`reports/rule_riskfix/health_check.md` 実験6)。
デモ運用(フェーズ8)開始前に解消すべき残課題:

1. ~~**ジャーナル永続化**(CLAUDE.md 第11条)~~ → **✅ 完了(2026-06-15)**。追記専用 JSONL
   ジャーナル(`src/infers/journal.py` の `JournalWriter`)を実装し、ライブループの全判断
   (SESSION / VERDICT+特徴量スナップショット / RISK_REJECT / FSM全遷移)を1行ずつ即 flush で
   永続化する(既定 `work/journal/<symbol>_<UTC日付>.jsonl`)。`python -m infers.journal replay`
   で要約+ゴールデン回帰検証(ルールゲートのセッションは記録済み特徴量を `judge_features` へ
   再投入し同一判断を確認)。`tests/test_journal.py` でカバー。
2. ~~**MT5Feedの堅牢化**~~ → **✅ 完了(2026-06-15)**。`data/mt5_feed.py`の`iter_closed()`に
   (a) 切断時の指数バックオフ再接続(`reconnect_base_s`〜`reconnect_max_s`、
   `max_reconnect_attempts`超過で`FeedError`を上位watchdogへ)、(b) ポーリング落伍・切断中の
   取りこぼしを`get_history`で補完する欠損バックフィル(合成ではなく実在確定足の補完)を実装。
   `tests/test_mt5_feed.py`(FakeMt5でMT5非依存に検証)でカバー。
3. ~~**リコンサイルの継続実行**~~ → **✅ 完了(2026-06-15)**。`LiveRunner.run()`が起動時に加え、
   (a) フィード再接続復帰直後(`MT5Feed.reconnect_count`の増分で検知、新規判断の前)と
   (b) 定期(`reconcile_every_bars`、既定12本=M5で1時間)に`reconcile()`を呼ぶ。結果は
   `RECONCILE`イベントとしてジャーナルへ記録。`snapshot()`を持たないSimブローカーでは
   no-op。`tests/test_integration.py::TestReconcileCadence`でカバー。これでフェーズ8の
   完了条件「リコンサイル不一致ゼロ」を稼働中も継続的に維持できる。

並行課題(デモ運用中も継続):
- **G3ニュース遮断**(entry-methodology.md G3、❌未実装): デモ初期は重要指標前後を
  オペレーターが手動監視・手動停止で代替する(手順は [demo-runbook.md](demo-runbook.md) §2)。
  実装完了後にゲートへ組み込む。安全停止(`Ctrl+C`→未約定取消+手仕舞い)は
  `main.run_live` に実装済み。
- **G1 200SMA方向確認**(entry-methodology.md G1、❌未実装): v1.0スコープ外(将来課題)。
- **ライブのウォームアップ自動化**: コールド起動で上位足インジケーターが揃うまで
  新規が出ない。事前に履歴を流し込むウォームアップは将来課題(runbook §1 に注意記載)。

---

## 11. リスクに関する注記

本システムは投資助言ではなく、手法の機械化である。レバレッジ取引は元本超過損失のリスクがあり、バックテストの成績は将来の成果を保証しない。ライブ投入は必ずデモ口座→最小ロットの段階を踏み、6.6のリスク上限を口座規模に対して保守的に設定すること。

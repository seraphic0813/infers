# インバリアント・レビュアー チェックリスト

差分レビュー時に機械的に検査する観点を、CLAUDE.md「コード記述の厳格なルール」の
**A(全手法共通の不変条件1〜15)**・**B(Narrow Focus 手法固有の契約)** から
具体的な grep/目視パターンへ落とし込んだもの。`/dev-pipeline` の③インバリアント・
レビュー段階、および通常の差分レビューで使う。

**使い方**: 対象の差分ファイル一覧に対し、該当する観点だけ実行すればよい(全観点を
無関係な差分にまで強制しない)。1件でも違反の疑いがあれば、該当箇所を明示して
差分作成者へ差し戻す。「よくある違反例」に一致しても、コメント・文字列リテラル・
表示専用コードなど無害な出現は除外してよいが、除外した場合は理由を一言添える。

---

## A-2 確定足主義(リペイント禁止)

- **観点**: 売買判断が対象TFの確定足クローズ時のみで行われているか。形成中バー・
  ティックのヒゲを判定に使っていないか。
- **よくある違反例**: 新規インジケーター計算やシグナル判定の入力に、直近バーの
  `is_closed` チェックを経ずに `candles[-1]`(形成中バー)を直接使う。あるいは
  `confirmed_at` を確認せずにスイング/波カウントを更新する。
- **確認コマンド例**:
  ```
  grep -rn "is_closed" src/infers/strategies/<対象手法>/ | grep -v test
  grep -rn "confirmed_at" src/infers/strategies/<対象手法>/provider.py
  ```
  差分に新しい「バー参照」箇所が増えていたら、上記のいずれかのガードを経由しているか
  目視で確認する。

## A-3 SLの単調性

- **観点**: SLを損失方向へ動かすコードパスが存在しないか。`modify_sl` 呼び出し箇所を
  総点検し、買いポジションでSLを下げる/売りポジションでSLを上げる分岐が無いか。
  hypothesis プロパティテストが当該 `ExecutionModel` に存在するか。
- **よくある違反例**: 建値化・トレール処理で `new_sl_int = max(...)`/`min(...)` の
  向きを取り違え、含み損方向にもSLが動く分岐が入る(特に売り/買いの符号を共通化した
  リファクタリングで発生しやすい)。
- **確認コマンド例**:
  ```
  grep -rn "modify_sl" src/infers/strategies/<対象手法>/execution.py
  grep -rln "sl_only_improves\|monotonic" tests/ | grep <対象手法>
  ```
  該当手法のテストファイルに SL 単調性の hypothesis プロパティテスト
  (`@given` + 買い/売り両方向のSL移動不変条件)が無ければ追加を要求する。

## A-5 float禁止(価格・数量)

- **観点**: 価格・数量の計算に float が混入していないか。float 同士の `==`/`<` 比較を
  していないか。表示・API境界(ログ出力・レポート生成・ブローカーAPIへのDecimal/str
  変換)は対象外。
- **よくある違反例**: 半分利確・ATR倍率計算で `volume * 0.5`・`price_int * 1.618` の
  ように整数ティック/ロットステップの世界に float 演算を直接持ち込む。
  `if sl_price == entry_price:` のような float 等価比較。
- **確認コマンド例**:
  ```
  grep -rn "float(" src/infers/strategies/<対象手法>/*.py | grep -v "test\|report\|display\|Decimal\|str("
  grep -rn "0\.5 \*\|\* 0\.5\b" src/infers/strategies/<対象手法>/*.py
  ```
  ヒットした行が価格・数量(`price_int`・`volume`・`*_ticks` 等)を扱っているか、
  それとも表示専用(HTMLレポート・ログ)かを目視で切り分ける。

## A-6 時刻はUTC固定

- **観点**: naive datetime(tz-aware でない `datetime`)やローカル時計を判定に
  使っていないか。
- **よくある違反例**: `datetime.now()`(引数なし=ローカルタイムゾーン)や
  `datetime.utcnow()`(tz情報なしのnaive UTC。比較時に事故りやすい)の使用。
  `datetime(2026, 1, 1, 9, 0)` のようなtz指定なしのリテラル構築。
- **確認コマンド例**:
  ```
  grep -rn "datetime.now()\|datetime.utcnow()" src/infers/ | grep -v test
  grep -rn "datetime(" src/infers/strategies/<対象手法>/*.py | grep -v "tzinfo\|timezone.utc\|UTC"
  ```

## A-7 スキーマ固定

- **観点**: モジュール間で受け渡すデータが frozen dataclass / pydantic モデルのみで、
  生 dict の引き回しが無いか。
- **よくある違反例**: `provider.py` の `on_candle` が `ProviderOutput` ではなく
  素の `dict` を返す、あるいは `execution.py` へ `dict` で特徴量を渡す関数シグネチャ
  を新設する。
- **確認コマンド例**:
  ```
  grep -rn "-> dict\|: dict\[" src/infers/strategies/<対象手法>/provider.py src/infers/strategies/<対象手法>/execution.py
  ```
  ヒットした戻り値/引数がモジュール境界(手法間・L0↔L2)を跨ぐものか、関数内部の
  一時変数(問題なし)かを目視で切り分ける。

## A-8 Enum一本の有限状態機械

- **観点**: ポジション状態が bool フラグの組合せで表現されていないか。
  `ExecutionModel` が Enum の状態遷移として実装されているか。
- **よくある違反例**: `is_open`・`is_half_closed`・`is_trailing` のような複数の
  bool 属性を並べて状態を表現し、組合せの一部が無効な状態(到達不能/矛盾)になり得る。
- **確認コマンド例**:
  ```
  grep -rn "self\.is_[a-z_]* = \(True\|False\)" src/infers/strategies/<対象手法>/execution.py
  grep -n "class.*State.*Enum\|class.*Phase.*Enum" src/infers/strategies/<対象手法>/execution.py
  ```
  bool 属性が2個以上見つかり、かつ同ファイルに状態を表す Enum クラスが無ければ違反を疑う。

## A-9 冪等性

- **観点**: 全注文操作(`place_limit`/`place_market`/`modify_sl`/`close_volume`/
  `cancel`)に決定論的 `client_order_id` が付与されているか。非決定論的なID生成
  (乱数・現在時刻ベースのUUID等)を使っていないか。
- **よくある違反例**: 新しい注文種別を追加した際に `client_order_id` を渡し忘れる。
  `uuid4()` や `time.time()` を種にしたID生成(リトライで毎回別IDになり冪等性が壊れる)。
- **確認コマンド例**:
  ```
  grep -rn "place_limit(\|place_market(\|close_volume(\|cancel(" src/infers/strategies/<対象手法>/execution.py
  grep -rn "uuid4()\|time.time()" src/infers/strategies/<対象手法>/execution.py
  ```
  各発注呼び出し箇所で `client_order_id=self._oid(...)` のような決定論的生成(
  `position_id` + 注文種別ラベルの組合せ等)を経由しているか確認する。

## A-10 イベントソーシング

- **観点**: 全判断が特徴量スナップショットとともに追記専用ジャーナルへ記録されて
  いるか。「ログに出してない判断分岐」が無いか。
- **よくある違反例**: 新しいゲート判定・エントリー却下分岐を追加したが、
  `journal`/`journal_writer` への記録呼び出しを追加し忘れる(判断はしているが
  ジャーナルに残らない=ゴールデンリプレイで再現できない)。
- **確認コマンド例**:
  ```
  grep -rn "def on_candle" src/infers/strategies/<対象手法>/provider.py
  grep -rln "journal" src/infers/strategies/<対象手法>/
  ```
  `on_candle` 内の新しい分岐(return 文)それぞれについて、呼び出し元
  (`TradingLoop`/`backtest/engine.py`)がジャーナル記録を経由するパスに乗っているか
  目視で追う。

## B契約(narrow_focus 限定)

- **観点**: `strategies/narrow_focus/` の変更において、(i) 防御調整(SL移動・建値化)
  のトリガーに含み益・PnL・pips を使っていないか(価格構造イベント限定の契約)、
  (ii) 半分利確が厳密に1回のみ実行されるか(`half_volume`/`runner_volume` が
  エントリー時固定で再計算されないか)。
- **よくある違反例**: `narrow_focus/execution.py` に `unrealized_pnl` や `pips` を
  引数に取る新しい建値化・SL移動関数を追加してしまう(A-4 の「別ロジックの手法は
  含み益トリガーを採用してよい」という自由は narrow_focus には適用されないため、
  これは narrow_focus 単体の契約違反になる)。半分利確フラグを毎確定足で
  再評価してしまい、条件を満たすたびに再度部分決済が走る実装。
- **確認コマンド例**:
  ```
  grep -n "pnl\|unrealized\|pips" src/infers/strategies/narrow_focus/execution.py
  grep -n "half_volume\|runner_volume\|half_closed\|half_taken" src/infers/strategies/narrow_focus/execution.py
  ```
  1つ目のコマンドでヒットがあれば(コメント・docstring以外)即座に違反を疑う
  (`narrow_focus/execution.py` は含み益・pipsを受け取るAPIを持たない構造が
  CLAUDE.md の担保機構そのものであるため)。2つ目は半分利確の実行が
  「済フラグ」でガードされ1回しか通らない構造になっているか確認する。

## depth50 不可侵(完了ゲート = 強い一致)

- **観点**: `strategies/narrow_focus/`・`core/`・`execution/` 配下への変更が
  1行でもある場合、完了ゲート(`/bit-check`)の**強い一致**
  (トレード列: PF 1.487396352 / 624件 / DD $325.77 / 勝率53.37% +
  ゲート統計: `CACHE:GO=3124`/`CACHE:NO_GO=1034`)の実測PASSを要求すること。
  レビューコメント・コミットメッセージだけで「変えていないはず」と自己申告させない。
- **よくある違反例**: `core/execution.py` の共通ヘルパーを「他手法のために」小さく
  リファクタリングし、「narrow_focus は呼び出し方を変えていないから大丈夫」と
  `/bit-check` を実行せずにマージする。プレースホルダ特徴量の配線変更など
  「一見 narrow_focus に無関係」な差分でも、ゲート統計(`CACHE:GO`/`NO_GO`)まで
  変わっていないか必ず確認する。
- **確認コマンド例**:
  ```
  git diff --name-only <base>...<head> -- src/infers/strategies/narrow_focus/ src/infers/core/ src/infers/execution/
  python tools/bit_check.py
  ```
  1つ目のコマンドで1件でもヒットしたら、2つ目の実行(または `/bit-check` Skill)の
  実測ログをレビュー記録に添付させる。実行していない/ログが無いPRは完了ゲート未達
  として差し戻す。

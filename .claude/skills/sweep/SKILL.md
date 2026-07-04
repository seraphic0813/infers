---
name: sweep
description: 任意の登録手法のprovider数値パラメータをOFAT/グリッドで掃引する汎用スイープ・ハーネス。ベースラインのビット一致自己検証・各構成の年次分解強制・非単調振動(過剰適合疑い)の自動検出を内蔵。新パラメータの採否判断に使う(CLAUDE.md定石6)。
---

# /sweep

`tools/sweep.py`: 手法非依存のパラメータ・スイープ・ハーネス。atr_trend_scalp v1.1
チューニング(vol_mult/tp1_atr_mult/tp2_atr_mult/trail_atr_mult の調整でPF1.17→1.55)で
使った「`run_backtest` と同一条件をビット一致で再現するインプロセス・スイープ」を
汎用ツールへ昇格したもの。

## 実行

```
python tools/sweep.py --data <parquet> \
  --provider-class <module:ClassName> \
  --strategy <registry登録名> \
  --ai-client rule|none \
  --golden-trades N --golden-pf "1.xxx" --golden-max-dd N \
  --base name1=value1 --base name2=value2 \
  --param sweep対象1=v1,v2,v3 [--param sweep対象2=v1,v2,v3 ...] \
  [--mode ofat|grid] [--from ISO] [--to ISO] [--last 1y]
```

- `--provider-class`: 実際にインスタンス化する provider クラス(例:
  `infers.strategies.atr_trend_scalp.provider:AtrTrendScalpProvider`)。
- `--strategy`: `execution_factory` を `infers.strategies.registry.get_strategy()` から
  引くためのレジストリ登録名(パラメータ自体はこのレジストリ経由では上書きできないため
  `--provider-class` を別途要求する設計)。
- `--golden-*`: 省略可。指定した場合、既定パラメータ(overrides無し)でのベースライン
  実行結果と完全一致するか自己検証し、不一致ならスイープ全体を中止する。
- `--mode ofat`(既定): 各 `--param` を他は `--base` 固定で個別に掃引(atr v1.1 と同じ
  OFAT方式)。`--mode grid`: 全 `--param` の直積(組み合わせ探索)。

## 出力の読み方

各構成の結果に **年次分解(`tools/annual_breakdown.py`)が自動適用**され、
以下の警告が出た構成は採用推奨から除外すること:
- 単年純益寄与>50%(一過性の相場依存)
- いずれかの暦年PF<0.9(平常年に負ける構造)

**OFAT掃引内で隣接値間のPFが2回以上向きを変えた(非単調に振動した)場合**、
「⚠️ PFが非単調に振動(過剰適合の疑い)」と明示される。この場合、単発の最良値を
「採用」と結論づけない(smc_bos の `rr_target` 論点Bで確認済みの過剰適合パターン。
CLAUDE.md 定石6)。

## 設計上の不変条件

- **一方向依存**: `infers.main`/`infers.backtest.engine` を import するのみ。
  バックテスト本体からこのツールへの import は無い(手法本体には一切影響しない)。
- `infers.main._build_gateway`/`_build_swap_config` をそのまま呼び出し、CLI
  (`python -m infers.main --mode backtest`)とゲート・スワップ構築ロジックを完全共有する
  (二重実装によるビット一致崩れを防ぐ)。
- トレード判定コード(provider/execution/engine)には一切変更を加えない。既存クラスを
  そのまま呼ぶだけ。

## 既知の制約

- provider が `symbol`/`tf` 以外に必須(既定値なし)のコンストラクタ引数を持つ場合は
  `--base` で明示的に値を与える必要がある(`build_provider_instance` は既定値の型を
  見て変換するため、既定値が無い引数は上書き指定必須)。
- 単一暦年しかないデータ(短期間スライス等)で黒字の場合、単年集中警告が自明に発火する
  (定石6の閾値そのものの挙動であり不具合ではない)。長期間データでのスイープを推奨する。

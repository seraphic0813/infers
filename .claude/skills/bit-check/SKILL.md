---
name: bit-check
description: pytest全件+depth50再現バックテストをワンコマンドで実行し、黄金値(強いビット一致=トレード列+AIゲート統計)と突合する。新手法追加・リファクタリング・AI層縮小など、depth50の挙動を変えていないことを担保する全変更の完了ゲート。
---

# /bit-check

CLAUDE.md「完了ゲート」節の検証をワンコマンド化する。すべての変更(新手法追加・リファクタリング・
AI層の縮小等)は、depth50 のトレード列に加え **AIゲート統計まで**変わっていないことを確認して
初めて完了とする(「強い一致」。改善プラン D1)。

## 実行

```
python tools/bit_check.py
```

- `--skip-pytest`: pytest をスキップして depth50 再現のみ検証(pytestを別途流している場合)。
- `--skip-backtest`: depth50 再現をスキップして pytest のみ検証(高速だが**完全な検証ではない**。
  コミット前は必ずフル実行すること)。

**実行時間**: depth50 再現バックテストのみで約20〜25分(XAUUSD M5・5年・354,393本)。
短縮版は用意しない(コミット前の完了ゲートは常にフル実行が必須。CLAUDE.md 定石5)。

## 黄金値(2026-07-03、`main` ブランチで確定・実測検証済み)

| 指標 | 値 |
|---|---|
| トレード数 | 624 |
| Profit Factor | 1.487396352 |
| 勝率 | 0.533653846(53.37%) |
| 最大DD | 32577 tick*steps($325.77) |
| AIゲート統計 | `CACHE:GO=3124` / `CACHE:NO_GO=1034` |

再現コマンド(`tools/bit_check.py` が内部で実行するのと同一):

```
python -m infers.main --mode backtest --data data/xauusd_m5.parquet \
  --verdict-cache work/cache/verdicts_depth50.sqlite3 \
  --macro-wave2 --depth-screen --depth-max 0.50 --no-fib-score
```

## 黄金値の更新について

黄金値は `tools/bit_check.py` のモジュール冒頭定数(`GOLDEN_*`)にハードコードされている。
**意図的に depth50 の挙動を変える変更(通常は発生しない)の場合のみ**更新し、コミットメッセージに
変更理由を明記すること。パラメータ実験・新手法追加・AI層縮小など、depth50 に触れないはずの変更で
黄金値がズレた場合は、**基準を更新せず**原因を特定して差し戻す(段階2.3 の移行規律と同じ)。

## 使うタイミング(必須)

- 新手法を `strategies/registry.py` に登録する前。
- L0/L1(core・indicators・execution 等)を触るリファクタリングの前後。
- AI層の撤去・縮小(改善プラン T6・T15)の前後 — これらのタスクでは特に、AIゲート統計の一致まで
  確認することが検証の核心。
- コミット前の最終確認。

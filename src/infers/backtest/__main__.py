"""CLAUDE.md 主要コマンド表のエントリポイント (薄いラッパー)。

  python -m infers.backtest run    --data data/xauusd_m5.parquet   # Pass3相当
  python -m infers.backtest judge  --batch --data ...              # Pass1
  python -m infers.backtest judge  --ingest results.jsonl          # 結果取込
  python -m infers.backtest replay --data ...                      # Pass2/3

実体は infers.main の --mode {backtest,judge,replay} に委譲する。
"""

from __future__ import annotations

import sys

from infers.main import main

_MODE = {"run": "backtest", "judge": "judge", "replay": "replay"}


def _argv(argv: list[str]) -> list[str]:
    if not argv or argv[0] not in _MODE:
        print("usage: python -m infers.backtest {run|judge|replay} [options]",
              file=sys.stderr)
        raise SystemExit(2)
    return ["--mode", _MODE[argv[0]], *argv[1:]]


if __name__ == "__main__":
    sys.exit(main(_argv(sys.argv[1:])))

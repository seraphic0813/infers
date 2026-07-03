"""CLAUDE.md 主要コマンド表のエントリポイント (薄いラッパー)。

  python -m infers.backtest run    --data data/xauusd_m5.parquet
  python -m infers.backtest replay --data ...

実体は infers.main の --mode {backtest,replay} に委譲する。
judge サブコマンド(LLM Batches API 経由の2パス収集)は改善プラン T6 で撤去済み
(トレード判定にLLMは現在不使用。CLAUDE.md A-12)。
"""

from __future__ import annotations

import sys

from infers.main import main

_MODE = {"run": "backtest", "replay": "replay"}


def _argv(argv: list[str]) -> list[str]:
    if not argv or argv[0] not in _MODE:
        print("usage: python -m infers.backtest {run|replay} [options]",
              file=sys.stderr)
        raise SystemExit(2)
    return ["--mode", _MODE[argv[0]], *argv[1:]]


if __name__ == "__main__":
    sys.exit(main(_argv(sys.argv[1:])))

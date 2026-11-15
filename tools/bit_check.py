"""完了ゲート(全変更の合否判定)のワンコマンド検証(改善プラン T2)。

`pytest` 全件 + depth50 再現バックテストを実行し、出力を黄金値(CLAUDE.md「完了ゲート」節)と
突合する。「強い一致」= トレード列(PF/トレード数/DD/勝率)に加え AIゲート統計
(CACHE:GO/CACHE:NO_GO の内訳)まで一致することを要求する(改善プラン D1)。

黄金値は 2026-07-03 に本コマンドの実行で確定(`main` ブランチ)。**意図的に基準を変更する場合のみ
更新し、変更理由をコミットメッセージに明記すること。**

使い方:
    python tools/bit_check.py            # pytest + depth50 再現の両方
    python tools/bit_check.py --skip-pytest   # depth50 再現のみ(pytestは別途流している場合)

実行時間: depth50 再現バックテストのみで約20〜25分(5年・354,393本)。
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass

# --- 黄金値(2026-07-03 main で確定。変更は意図的な基準更新時のみ) -----------

GOLDEN_TRADES = 624
GOLDEN_PROFIT_FACTOR = "1.487396352"
GOLDEN_WIN_RATE = "0.533653846"          # = 53.37%
GOLDEN_MAX_DD_TICK_STEPS = 32577          # = $325.77 ($0.01/tick*step)
GOLDEN_GATE_STATS = {"CACHE:GO": 3124, "CACHE:NO_GO": 1034}

DEPTH50_CMD = [
    sys.executable, "-m", "infers.main", "--mode", "backtest",
    "--data", "data/xauusd_m5.parquet",
    "--verdict-cache", "work/cache/verdicts_depth50.sqlite3",
    "--macro-wave2", "--depth-screen", "--depth-max", "0.50", "--no-fib-score",
]


@dataclass
class ParsedBacktest:
    trades: int | None = None
    profit_factor: str | None = None
    win_rate: str | None = None
    max_dd_tick_steps: int | None = None
    gate_stats: dict[str, int] | None = None


def parse_backtest_output(text: str) -> ParsedBacktest:
    result = ParsedBacktest()
    m = re.search(r"bars=\d+\s+trades=(\d+)", text)
    if m:
        result.trades = int(m.group(1))
    m = re.search(r"profit_factor=([\d.]+)\s+win_rate=([\d.]+)", text)
    if m:
        result.profit_factor = m.group(1)
        result.win_rate = m.group(2)
    m = re.search(r"max_dd=(\d+)", text)
    if m:
        result.max_dd_tick_steps = int(m.group(1))
    m = re.search(r"ai_gateway:\s*(.+)", text)
    if m:
        stats: dict[str, int] = {}
        for pair in m.group(1).split():
            key, _, val = pair.partition("=")
            if key and val:
                stats[key] = int(val)
        result.gate_stats = stats
    return result


def compare_to_golden(parsed: ParsedBacktest) -> list[str]:
    """不一致の一覧を返す(空なら完全一致)。"""
    diffs: list[str] = []
    if parsed.trades != GOLDEN_TRADES:
        diffs.append(f"trades: got {parsed.trades}, expected {GOLDEN_TRADES}")
    if parsed.profit_factor != GOLDEN_PROFIT_FACTOR:
        diffs.append(f"profit_factor: got {parsed.profit_factor}, expected {GOLDEN_PROFIT_FACTOR}")
    if parsed.win_rate != GOLDEN_WIN_RATE:
        diffs.append(f"win_rate: got {parsed.win_rate}, expected {GOLDEN_WIN_RATE}")
    if parsed.max_dd_tick_steps != GOLDEN_MAX_DD_TICK_STEPS:
        diffs.append(
            f"max_dd_tick_steps: got {parsed.max_dd_tick_steps}, "
            f"expected {GOLDEN_MAX_DD_TICK_STEPS}"
        )
    if parsed.gate_stats != GOLDEN_GATE_STATS:
        diffs.append(f"gate_stats: got {parsed.gate_stats}, expected {GOLDEN_GATE_STATS}")
    return diffs


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    """子プロセスの出力を UTF-8 に固定して読む。

    既定(text=True のみ)では Windows のロケール(cp932)で復号され、子の出力に
    cp932 で復号できないバイトが混じると読み取りスレッドが落ちて stdout が None になり、
    検証結果を判定できないまま失敗していた(2026-11 に2回発生)。
    """
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env)


def run_pytest() -> tuple[bool, str]:
    proc = _run([sys.executable, "-m", "pytest", "-q"])
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output


def run_depth50_backtest() -> tuple[bool, str]:
    proc = _run(DEPTH50_CMD)
    output = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode == 0, output


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-pytest", action="store_true",
                        help="pytestをスキップしdepth50再現のみ検証する")
    parser.add_argument("--skip-backtest", action="store_true",
                        help="depth50再現をスキップしpytestのみ検証する(高速だが完全な検証ではない)")
    args = parser.parse_args(argv)

    overall_ok = True

    if not args.skip_pytest:
        print("=== pytest 全件 ===")
        ok, output = run_pytest()
        tail = "\n".join(output.strip().splitlines()[-15:])
        print(tail)
        print("PASS" if ok else "FAIL")
        overall_ok = overall_ok and ok
    else:
        print("=== pytest: スキップ ===")

    if not args.skip_backtest:
        print("\n=== depth50 再現バックテスト(約20〜25分) ===")
        ok, output = run_depth50_backtest()
        print(output.strip().splitlines()[-1] if output.strip() else "(出力なし)")
        if not ok:
            print("FAIL: バックテストがゼロ以外の終了コードで終了しました")
            overall_ok = False
        else:
            parsed = parse_backtest_output(output)
            diffs = compare_to_golden(parsed)
            if diffs:
                print("FAIL: 黄金値との不一致(強い一致=ゲート統計込みで判定):")
                for d in diffs:
                    print(f"  - {d}")
                overall_ok = False
            else:
                print(
                    "PASS: depth50 強いビット一致"
                    f" (trades={parsed.trades} PF={parsed.profit_factor}"
                    f" win_rate={parsed.win_rate} max_dd={parsed.max_dd_tick_steps}"
                    f" gate={parsed.gate_stats})"
                )
    else:
        print("\n=== depth50 再現バックテスト: スキップ ===")

    print(f"\n=== 完了ゲート総合判定: {'PASS' if overall_ok else 'FAIL'} ===")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())

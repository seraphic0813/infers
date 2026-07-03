"""年次分解ツール(手法非依存・読み取り専用)。

`report.html` が読む `report_data.js`(`window.BT = {...};`)からトレード列を取り出し、
暦年(UTC の決済年)ごとの トレード数/PF/勝率/純益/年内最大DD を算出する。

**トレード判定コードには一切触れない**(生成済みレポートの解析のみ)。
CLAUDE.md 定石6「新パラメータ・新フィルタの採否は全期間1本の指標で判断しない。
年次分解+事前宣言基準で判断する」のツール化(改善プラン T3)。

使い方:
    python tools/annual_breakdown.py reports/rule_depth50
    python tools/annual_breakdown.py reports/smc_bos_full --json

警告(過剰適合/一過性依存の疑い、事前宣言済みの機械判定):
    (a) 単一暦年の純益寄与が全期間純益の50%を超える
    (b) いずれかの暦年で PF < 0.9
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path

SINGLE_YEAR_CONTRIBUTION_WARN_PCT = 50.0
ANNUAL_PF_FLOOR_WARN = 0.9


def load_report_data(report_dir: Path) -> dict:
    """`report_data.js`(`window.BT = {...};`)を読み込み JSON へ変換する。"""
    path = report_dir / "report_data.js"
    if not path.exists():
        raise FileNotFoundError(f"report_data.js not found: {path}")
    text = path.read_text(encoding="utf-8")
    start = text.index("{")
    end = text.rindex("}")
    return json.loads(text[start:end + 1])


def _trade_pnl(trade: dict) -> float:
    """USD建て損益。pnl_usd が無い手法(生ticksのみ)は pnl_ts を代替に使う。"""
    if "pnl_usd" in trade and trade["pnl_usd"] is not None:
        return float(trade["pnl_usd"])
    return float(trade.get("pnl_ts", 0))


def _exit_year(trade: dict) -> int:
    ts = trade["exit_time"]
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).year


def annual_breakdown(data: dict) -> dict[int, dict]:
    """暦年 → {trades, wins, losses, gross_win, gross_loss, net, max_dd} を返す。

    最大DD は「その年に決済されたトレードの累積損益列」内でのピーク→トラフの
    最大下落幅(年内リセット。前年からの持ち越しDDは含めない=各年単体の健全性を見る)。
    """
    trades = sorted(data["trades"], key=lambda t: t["exit_time"])
    by_year: dict[int, list[dict]] = defaultdict(list)
    for t in trades:
        by_year[_exit_year(t)].append(t)

    result: dict[int, dict] = {}
    for year, yr_trades in by_year.items():
        gross_win = 0.0
        gross_loss = 0.0
        running = 0.0
        peak = 0.0
        max_dd = 0.0
        wins = 0
        for t in yr_trades:
            pnl = _trade_pnl(t)
            if pnl >= 0:
                gross_win += pnl
                wins += 1
            else:
                gross_loss += -pnl
            running += pnl
            peak = max(peak, running)
            max_dd = max(max_dd, peak - running)
        n = len(yr_trades)
        pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
        net = gross_win - gross_loss
        result[year] = {
            "trades": n,
            "wins": wins,
            "win_rate_pct": (100.0 * wins / n) if n else 0.0,
            "profit_factor": pf,
            "net_profit": net,
            "max_drawdown": max_dd,
        }
    return result


def evaluate_warnings(yearly: dict[int, dict]) -> list[str]:
    """事前宣言済みの機械警告(過剰適合/一過性依存の疑い)。"""
    warnings: list[str] = []
    total_net = sum(y["net_profit"] for y in yearly.values())
    if total_net > 0:
        for year, y in sorted(yearly.items()):
            share = 100.0 * y["net_profit"] / total_net
            if share > SINGLE_YEAR_CONTRIBUTION_WARN_PCT:
                warnings.append(
                    f"(a) 単年集中: {year}年の純益寄与が {share:.1f}% "
                    f"(> {SINGLE_YEAR_CONTRIBUTION_WARN_PCT:.0f}%)。過剰適合/一過性依存の疑い。"
                )
    for year, y in sorted(yearly.items()):
        if y["profit_factor"] < ANNUAL_PF_FLOOR_WARN:
            warnings.append(
                f"(b) 年次フロア割れ: {year}年の PF={y['profit_factor']:.3f} "
                f"(< {ANNUAL_PF_FLOOR_WARN})。平常年に負ける構造の疑い。"
            )
    return warnings


def format_table(yearly: dict[int, dict]) -> str:
    lines = ["| 年 | トレード数 | PF | 勝率 | 純益 | 年内最大DD |", "|---|---|---|---|---|---|"]
    for year in sorted(yearly):
        y = yearly[year]
        pf_str = f"{y['profit_factor']:.3f}" if y["profit_factor"] != float("inf") else "inf"
        lines.append(
            f"| {year} | {y['trades']} | {pf_str} | {y['win_rate_pct']:.2f}% | "
            f"{y['net_profit']:+.2f} | {y['max_drawdown']:.2f} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windowsコンソールの文字化け対策
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report_dir", type=Path, help="reports/<name>/ ディレクトリ")
    parser.add_argument("--json", action="store_true", help="JSON形式で出力(warnings含む)")
    args = parser.parse_args(argv)

    data = load_report_data(args.report_dir)
    yearly = annual_breakdown(data)
    warnings = evaluate_warnings(yearly)

    if args.json:
        payload = {
            "yearly": {str(y): v for y, v in sorted(yearly.items())},
            "warnings": warnings,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_table(yearly))
        print()
        if warnings:
            print("## 警告")
            for w in warnings:
                print(f"- {w}")
        else:
            print("## 警告なし(過剰適合の兆候は検出されず)")

    return 1 if warnings else 0


if __name__ == "__main__":
    sys.exit(main())

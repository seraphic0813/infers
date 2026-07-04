"""手法間相関測定(改善プランT11)。

3手法(narrow_focus/depth50・atr_trend_scalp・smc_bos)のバックテストレポート
(report_data.js)から日次損益系列を構築し、ペア相関・ポートフォリオ合成DD・
最悪同時損失日を算出する。「口座分離=リスク分離」という暗黙仮定を検証する。

**前提・限界**:
- 各レポートは独立した初期資本$10,000・固定ロットのバックテストであり、実口座の
  資金配分・レバレッジは反映していない(合成DDは「等倍で単純合算した場合」の参考値)。
- `smc_bos` はライブ凍結中(2026-07-03、年次フロア不合格)だが、相関測定には
  参考として含める(改善レポート「集約リスク」節の方針どおり)。
- 3レポートの対象期間はほぼ同一(2021-06〜2026-06)だが厳密には数日ずれる
  (データ取得タイミングの差)。日次アラインメント(和集合・欠損日=0)で吸収する。

トレード判定コードには一切触れない(レポートの読み取りのみ)。

使い方:
    python tools/portfolio_correlation.py
    python tools/portfolio_correlation.py --json
"""

from __future__ import annotations

import argparse
import datetime as dt
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from annual_breakdown import _trade_pnl, load_report_data  # noqa: E402

DEFAULT_REPORTS: dict[str, str] = {
    "depth50": "reports/rule_depth50",
    "atr_trend_scalp": "reports/atr_trend_scalp_full",
    "smc_bos(凍結中)": "reports/smc_bos_full",
}


def daily_pnl(report_dir: Path) -> dict[dt.date, float]:
    """report_data.js から UTC日別の合計損益(USD相当)を構築する。"""
    data = load_report_data(report_dir)
    out: dict[dt.date, float] = {}
    for t in data["trades"]:
        day = dt.datetime.fromtimestamp(t["exit_time"], tz=dt.timezone.utc).date()
        out[day] = out.get(day, 0.0) + _trade_pnl(t)
    return out


def align(series: dict[str, dict[dt.date, float]]
         ) -> tuple[list[dt.date], dict[str, list[float]]]:
    """全戦略の日付集合の和集合でアラインメントし、欠損日は0で埋める。"""
    if not series:
        return [], {}
    all_days = sorted(set().union(*(s.keys() for s in series.values())))
    aligned = {name: [s.get(d, 0.0) for d in all_days] for name, s in series.items()}
    return all_days, aligned


def pairwise_correlation(aligned: dict[str, list[float]]) -> dict[tuple[str, str], float]:
    """日次損益系列のペア相関(Pearson)。分散ゼロ(無取引期間等)は NaN。"""
    names = list(aligned)
    result: dict[tuple[str, str], float] = {}
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            try:
                result[(a, b)] = statistics.correlation(aligned[a], aligned[b])
            except statistics.StatisticsError:
                result[(a, b)] = float("nan")
    return result


def combo_drawdown(days: list[dt.date], aligned: dict[str, list[float]]) -> dict:
    """等倍単純合算ポートフォリオの最大DD・最悪単日損失・共倒れ頻度を算出する。"""
    n = len(days)
    if n == 0:
        return {}
    names = list(aligned)
    portfolio = [sum(aligned[name][i] for name in names) for i in range(n)]

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    max_dd_date: dt.date | None = None
    for day, pnl in zip(days, portfolio):
        equity += pnl
        peak = max(peak, equity)
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
            max_dd_date = day

    worst_idx = min(range(n), key=lambda i: portfolio[i])
    worst_day = days[worst_idx]
    worst_breakdown = {name: aligned[name][worst_idx] for name in names}

    loss_days = [i for i in range(n) if any(aligned[name][i] < 0 for name in names)]
    co_loss_days = [i for i in loss_days
                   if sum(1 for name in names if aligned[name][i] < 0) >= 2]

    return {
        "max_drawdown_usd": round(max_dd, 2),
        "max_drawdown_date": max_dd_date.isoformat() if max_dd_date else None,
        "worst_single_day": worst_day.isoformat(),
        "worst_single_day_total_usd": round(portfolio[worst_idx], 2),
        "worst_single_day_breakdown": {k: round(v, 2) for k, v in worst_breakdown.items()},
        "loss_days": len(loss_days),
        "co_loss_days": len(co_loss_days),
        "co_loss_pct": (100.0 * len(co_loss_days) / len(loss_days)) if loss_days else 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", action="append", default=[], metavar="name=path",
                        help="'name=report_dir' (繰り返し可)。省略時は既定3手法を使用")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report_paths: dict[str, str] = dict(DEFAULT_REPORTS)
    for raw in args.report:
        name, _, path = raw.partition("=")
        report_paths[name] = path

    series = {name: daily_pnl(Path(path)) for name, path in report_paths.items()}
    days, aligned = align(series)
    corr = pairwise_correlation(aligned)
    combo = combo_drawdown(days, aligned)

    if args.json:
        import json
        payload = {
            "correlation": {f"{a}|{b}": v for (a, b), v in corr.items()},
            "combo": combo,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"対象手法: {', '.join(report_paths)}")
    print(f"アラインメント後の日数: {len(days)}"
          f"({days[0].isoformat()} 〜 {days[-1].isoformat()})" if days else "0")
    print()
    print("## ペア相関(日次損益、Pearson)")
    for (a, b), v in corr.items():
        v_str = "N/A" if v != v else f"{v:+.3f}"   # NaN check
        print(f"  {a} × {b}: {v_str}")
    print()
    print("## ポートフォリオ合成DD(等倍単純合算・参考値)")
    for k, v in combo.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

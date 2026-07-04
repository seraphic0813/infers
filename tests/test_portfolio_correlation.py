"""tools/portfolio_correlation.py のテスト(改善プランT11)。

合成の report_data.js フィクスチャで相関・合成DD・共倒れ日の計算ロジックを検証する。
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from portfolio_correlation import (  # noqa: E402
    align, combo_drawdown, daily_pnl, pairwise_correlation,
)

UTC = dt.timezone.utc


def _write_report(dirpath: Path, trades: list[dict]) -> None:
    dirpath.mkdir(parents=True, exist_ok=True)
    payload = {"summary": {}, "trades": trades}
    (dirpath / "report_data.js").write_text(
        "window.BT = " + json.dumps(payload) + ";", encoding="utf-8")


def _ts(y: int, m: int, d: int) -> int:
    return int(dt.datetime(y, m, d, 12, tzinfo=UTC).timestamp())


class TestDailyPnl:
    def test_aggregates_multiple_trades_same_day(self, tmp_path):
        report_dir = tmp_path / "r1"
        _write_report(report_dir, [
            {"exit_time": _ts(2026, 1, 1), "pnl_usd": "10.00"},
            {"exit_time": _ts(2026, 1, 1), "pnl_usd": "-3.00"},
            {"exit_time": _ts(2026, 1, 2), "pnl_usd": "5.00"},
        ])
        series = daily_pnl(report_dir)
        assert series[dt.date(2026, 1, 1)] == 7.0
        assert series[dt.date(2026, 1, 2)] == 5.0

    def test_falls_back_to_pnl_ts_when_pnl_usd_absent(self, tmp_path):
        report_dir = tmp_path / "r2"
        _write_report(report_dir, [{"exit_time": _ts(2026, 1, 1), "pnl_ts": 100}])
        series = daily_pnl(report_dir)
        assert series[dt.date(2026, 1, 1)] == 100.0


class TestAlign:
    def test_fills_missing_days_with_zero(self):
        series = {
            "a": {dt.date(2026, 1, 1): 10.0, dt.date(2026, 1, 3): -5.0},
            "b": {dt.date(2026, 1, 2): 2.0},
        }
        days, aligned = align(series)
        assert days == [dt.date(2026, 1, 1), dt.date(2026, 1, 2), dt.date(2026, 1, 3)]
        assert aligned["a"] == [10.0, 0.0, -5.0]
        assert aligned["b"] == [0.0, 2.0, 0.0]

    def test_empty_series_returns_empty(self):
        assert align({}) == ([], {})


class TestPairwiseCorrelation:
    def test_perfectly_correlated_series(self):
        aligned = {"a": [1.0, 2.0, 3.0, 4.0], "b": [2.0, 4.0, 6.0, 8.0]}
        corr = pairwise_correlation(aligned)
        assert corr[("a", "b")] == 1.0

    def test_uncorrelated_orthogonal_series(self):
        # a と b が互いに素な日にしか動かない(共分散ゼロに近い)場合、
        # 相関はゼロに近い値になる(厳密な直交級数)
        aligned = {"a": [1.0, -1.0, 1.0, -1.0], "b": [1.0, 1.0, -1.0, -1.0]}
        corr = pairwise_correlation(aligned)
        assert abs(corr[("a", "b")]) < 1e-9

    def test_zero_variance_series_yields_nan(self):
        aligned = {"a": [0.0, 0.0, 0.0], "b": [1.0, 2.0, 3.0]}
        corr = pairwise_correlation(aligned)
        v = corr[("a", "b")]
        assert v != v   # NaN


class TestComboDrawdown:
    def test_max_drawdown_and_worst_day(self):
        days = [dt.date(2026, 1, i) for i in range(1, 5)]
        aligned = {
            "a": [10.0, -20.0, 5.0, 0.0],
            "b": [0.0, -5.0, 0.0, 3.0],
        }
        # portfolio: [10, -25, 5, 3] → equity [10, -15, -10, -7]
        # peak=10 (day1), max_dd = 10-(-15)=25 (day2)
        result = combo_drawdown(days, aligned)
        assert result["max_drawdown_usd"] == 25.0
        assert result["max_drawdown_date"] == "2026-01-02"
        assert result["worst_single_day"] == "2026-01-02"
        assert result["worst_single_day_total_usd"] == -25.0
        assert result["worst_single_day_breakdown"] == {"a": -20.0, "b": -5.0}

    def test_co_loss_day_counting(self):
        days = [dt.date(2026, 1, i) for i in range(1, 4)]
        aligned = {
            "a": [-1.0, -1.0, 1.0],    # 損失日: day1, day2
            "b": [1.0, -1.0, -1.0],    # 損失日: day2, day3
        }
        # 損失日(いずれか負): day1,day2,day3 = 3件。共倒れ(両方負): day2のみ = 1件
        result = combo_drawdown(days, aligned)
        assert result["loss_days"] == 3
        assert result["co_loss_days"] == 1
        assert abs(result["co_loss_pct"] - (100.0 / 3)) < 1e-9

    def test_empty_input_returns_empty_dict(self):
        assert combo_drawdown([], {}) == {}

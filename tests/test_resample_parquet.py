"""tools/resample_parquet.py のテスト(改善プラン T16 / narrow_focus_mtf 設計書 §2.1)。

バケット境界(正時丸め)・OHLCV集約の正しさ・不完全バケット破棄・決定論性・
週末ギャップ(空バケットが生成されないこと)を検証する。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from infers.core.models import Timeframe  # noqa: E402

from resample_parquet import _floor_to_bucket, resample  # noqa: E402

UTC = timezone.utc


def _row(t: datetime, o: int, h: int, l: int, c: int, v: int, symbol: str = "XAUUSD",
        tf: str = "M5") -> dict:
    return {"symbol": symbol, "tf": tf, "time": t, "o_int": o, "h_int": h,
            "l_int": l, "c_int": c, "volume": v}


def _df(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema={
        "symbol": pl.Utf8, "tf": pl.Utf8, "time": pl.Datetime(time_unit="us", time_zone="UTC"),
        "o_int": pl.Int64, "h_int": pl.Int64, "l_int": pl.Int64, "c_int": pl.Int64,
        "volume": pl.Int64,
    })


class TestFloorToBucket:
    def test_floors_to_hour_start(self):
        t = datetime(2026, 6, 1, 10, 37, 12, tzinfo=UTC)
        assert _floor_to_bucket(t, 3600) == datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)

    def test_already_on_boundary_unchanged(self):
        t = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
        assert _floor_to_bucket(t, 3600) == t

    def test_last_bar_of_hour_floors_to_same_bucket(self):
        t = datetime(2026, 6, 1, 10, 55, 0, tzinfo=UTC)
        assert _floor_to_bucket(t, 3600) == datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)


class TestOhlcvAggregation:
    def test_h1_aggregates_12_m5_bars_correctly(self):
        """10:00-10:55の12本のM5バーが1本のH1バーへ正しく集約されること。
        + 次時間帯(11:00台)のバーを1本足して10:00バケットの完成を保証する
        (末尾バケット破棄の対象は11:00側)。
        """
        base = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
        rows = []
        for i in range(12):
            t = base + timedelta(minutes=5 * i)
            rows.append(_row(t, o=100 + i, h=110 + i, l=90 - i, c=105 + i, v=10))
        # 次バケット分 (これが末尾として破棄される)
        rows.append(_row(base + timedelta(hours=1), o=999, h=999, l=999, c=999, v=1))

        out = resample(_df(rows), Timeframe.H1)
        assert out.height == 1
        row = out.row(0, named=True)
        assert row["time"] == base
        assert row["o_int"] == rows[0]["o_int"]          # 先頭バーの o
        assert row["c_int"] == rows[11]["c_int"]         # 末尾バーの c
        assert row["h_int"] == max(r["h_int"] for r in rows[:12])
        assert row["l_int"] == min(r["l_int"] for r in rows[:12])
        assert row["volume"] == sum(r["volume"] for r in rows[:12])
        assert row["tf"] == "H1"
        assert row["symbol"] == "XAUUSD"


class TestIncompleteBucketDiscarded:
    def test_trailing_incomplete_bucket_is_dropped(self):
        base = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
        rows = [
            _row(base, o=100, h=110, l=90, c=105, v=10),
            _row(base + timedelta(minutes=5), o=105, h=115, l=95, c=110, v=10),
            # 次のバケット (11:00台) は1本のみ = 不完全 → 破棄されるはず
            _row(base + timedelta(hours=1), o=200, h=210, l=190, c=205, v=5),
        ]
        out = resample(_df(rows), Timeframe.H1)
        assert out.height == 1
        assert out.row(0, named=True)["time"] == base

    def test_single_bucket_input_yields_empty_output(self):
        """入力が1バケット分のみの場合、それは末尾=不完全として破棄され0行になる。"""
        base = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
        rows = [_row(base, o=100, h=110, l=90, c=105, v=10)]
        out = resample(_df(rows), Timeframe.H1)
        assert out.height == 0


class TestDeterminism:
    def test_same_input_yields_same_output(self):
        base = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
        rows = [_row(base + timedelta(minutes=5 * i), o=100 + i, h=110 + i, l=90 - i,
                     c=105 + i, v=10) for i in range(50)]
        df = _df(rows)
        out1 = resample(df, Timeframe.H1)
        out2 = resample(df, Timeframe.H1)
        assert out1.equals(out2)

    def test_shuffled_input_yields_same_output(self):
        """入力の行順序に依存しない(内部でソートする)。"""
        base = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
        rows = [_row(base + timedelta(minutes=5 * i), o=100 + i, h=110 + i, l=90 - i,
                     c=105 + i, v=10) for i in range(24)]
        df_sorted = _df(rows)
        df_shuffled = _df(list(reversed(rows)))
        out_sorted = resample(df_sorted, Timeframe.H1)
        out_shuffled = resample(df_shuffled, Timeframe.H1)
        assert out_sorted.equals(out_shuffled)


class TestWeekendGap:
    def test_no_synthetic_bucket_created_across_gap(self):
        """金曜終値の後、週末を挟んで月曜始値まで空バケットが生成されないこと。"""
        fri = datetime(2026, 6, 5, 20, 0, 0, tzinfo=UTC)   # 金曜
        mon = datetime(2026, 6, 8, 1, 0, 0, tzinfo=UTC)    # 月曜 (週末ギャップ)
        rows = [
            _row(fri, o=100, h=110, l=90, c=105, v=10),
            _row(fri + timedelta(minutes=5), o=105, h=115, l=95, c=110, v=10),
            _row(mon, o=200, h=210, l=190, c=205, v=10),
            _row(mon + timedelta(minutes=5), o=205, h=215, l=195, c=210, v=10),
            # 末尾破棄用のダミーバケット
            _row(mon + timedelta(hours=1), o=300, h=310, l=290, c=305, v=1),
        ]
        out = resample(_df(rows), Timeframe.H1)
        times = out["time"].to_list()
        assert fri.replace(minute=0) in times
        assert mon.replace(minute=0) in times
        # 金曜と月曜の間に合成バケットが挟まっていない (連続する2バケットのみ)
        assert out.height == 2
        gap_hours = (times[1] - times[0]).total_seconds() / 3600
        assert gap_hours > 1  # 週末分のギャップがそのまま反映されている

    def test_multi_symbol_input_raises(self):
        base = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
        rows = [
            _row(base, o=100, h=110, l=90, c=105, v=10, symbol="XAUUSD"),
            _row(base + timedelta(minutes=5), o=105, h=115, l=95, c=110, v=10, symbol="BTCUSD"),
        ]
        with pytest.raises(ValueError):
            resample(_df(rows), Timeframe.H1)

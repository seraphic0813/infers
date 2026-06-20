"""バックテスト期間スライス (段階2.4) のテスト。"""

from datetime import datetime, timedelta, timezone

import pytest

from infers.backtest.slicing import parse_relative, slice_candles
from infers.core.models import Candle, Timeframe

UTC = timezone.utc
T0 = datetime(2021, 1, 1, 0, 0, tzinfo=UTC)


def mk_candle(i: int) -> Candle:
    t = T0 + i * Timeframe.M5.duration
    return Candle(symbol="XAUUSD", tf=Timeframe.M5, open_time=t,
                  o_int=100, h_int=101, l_int=99, c_int=100, volume=1, is_closed=True)


# 5年分相当(1年=年365日換算で5倍の本数)を模した連続足
DAY_BARS = 288  # M5 1日 = 24*60/5
ALL_CANDLES = [mk_candle(i) for i in range(DAY_BARS * 365 * 5)]


def test_no_filter_returns_all():
    out = slice_candles(ALL_CANDLES)
    assert len(out) == len(ALL_CANDLES)


def test_last_1y_returns_final_year_only():
    out = slice_candles(ALL_CANDLES, last="1y")
    assert out[0].open_time >= ALL_CANDLES[-1].open_time - timedelta(days=365)
    assert out[-1].open_time == ALL_CANDLES[-1].open_time
    # 終端1年分のみ (誤差なく365日分の本数)
    assert len(out) == DAY_BARS * 365 + 1


def test_explicit_from_to_range():
    start = T0 + timedelta(days=10)
    end = T0 + timedelta(days=20)
    out = slice_candles(ALL_CANDLES, start=start, end=end)
    assert out[0].open_time == start
    assert out[-1].open_time <= end
    assert all(start <= c.open_time <= end for c in out)


def test_naive_datetime_treated_as_utc():
    start = datetime(2021, 1, 10)  # naive
    out = slice_candles(ALL_CANDLES, start=start)
    assert out[0].open_time == T0 + timedelta(days=9)


def test_empty_input_returns_empty():
    assert slice_candles([]) == []


def test_out_of_range_returns_empty():
    start = T0 + timedelta(days=10_000)
    assert slice_candles(ALL_CANDLES, start=start) == []


@pytest.mark.parametrize("token,expected_days", [
    ("1y", 365), ("6m", 180), ("90d", 90), ("4w", 28),
])
def test_parse_relative(token, expected_days):
    assert parse_relative(token) == timedelta(days=expected_days)


def test_parse_relative_invalid():
    with pytest.raises(ValueError):
        parse_relative("abc")

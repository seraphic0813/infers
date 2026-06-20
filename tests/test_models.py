"""データモデルの単体テスト (CLAUDE.md 第2・6・7条の担保)。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from infers.core.models import Candle, SymbolSpec, Timeframe

GOLD = SymbolSpec(name="XAUUSD", tick_size=Decimal("0.01"), lot_step=Decimal("0.01"), digits=2)
UTC = timezone.utc


class TestSymbolSpec:
    def test_to_ticks_roundtrip(self):
        ticks = GOLD.to_ticks(Decimal("1950.37"))
        assert ticks == 195037
        assert GOLD.from_ticks(ticks) == Decimal("1950.37")

    def test_float_boundary_conversion(self):
        # MT5境界のfloatはreprを介して正確に変換される
        assert GOLD.float_to_ticks(1950.37) == 195037
        assert GOLD.float_to_ticks(0.07) == 7  # 2進浮動小数で表現できない値

    def test_non_multiple_price_raises(self):
        with pytest.raises(ValueError, match="not a multiple"):
            GOLD.to_ticks(Decimal("1950.375"))

    def test_frozen(self):
        with pytest.raises(Exception):
            GOLD.tick_size = Decimal("0.1")  # type: ignore[misc]


class TestCandle:
    def _mk(self, **kw):
        base = dict(
            symbol="XAUUSD",
            tf=Timeframe.H1,
            open_time=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
            o_int=195000, h_int=195100, l_int=194900, c_int=195050,
            volume=100, is_closed=True,
        )
        base.update(kw)
        return Candle(**base)

    def test_valid_candle(self):
        c = self._mk()
        assert c.close_time == datetime(2026, 6, 1, 13, 0, tzinfo=UTC)

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValueError, match="tz-aware UTC"):
            self._mk(open_time=datetime(2026, 6, 1, 12, 0))

    def test_non_utc_rejected(self):
        jst = timezone(timedelta(hours=9))
        with pytest.raises(ValueError, match="tz-aware UTC"):
            self._mk(open_time=datetime(2026, 6, 1, 12, 0, tzinfo=jst))

    def test_high_below_low_rejected(self):
        with pytest.raises(ValueError, match="high"):
            self._mk(h_int=194800)

    def test_close_outside_range_rejected(self):
        with pytest.raises(ValueError, match="close outside"):
            self._mk(c_int=195200)

    def test_from_decimal(self):
        c = Candle.from_decimal(
            GOLD, tf=Timeframe.M5,
            open_time=datetime(2026, 6, 1, tzinfo=UTC),
            o="1950.00", h="1951.00", l="1949.50", c="1950.37", volume=42,
        )
        assert (c.o_int, c.h_int, c.l_int, c.c_int) == (195000, 195100, 194950, 195037)
        assert c.is_closed is True

    def test_frozen(self):
        c = self._mk()
        with pytest.raises(Exception):
            c.c_int = 0  # type: ignore[misc]


class TestTimeframe:
    def test_durations(self):
        assert Timeframe.M5.duration == timedelta(minutes=5)
        assert Timeframe.W1.duration == timedelta(weeks=1)

    def test_m30_duration(self):
        # smc_bos 手法の判定TF (spec.md §5.6)。M15 と H1 の中間。
        assert Timeframe.M30.duration == timedelta(minutes=30)
        assert Timeframe.M15.duration < Timeframe.M30.duration < Timeframe.H1.duration

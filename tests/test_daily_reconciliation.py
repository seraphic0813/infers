"""tools/daily_reconciliation.py のテスト(改善プランT13 OPS-3)。

MetaTrader5 はテスト環境で実インストール済みのため、実接続を回避するよう
sys.modules へフェイクモジュールを注入して検証する(実MT5には一切触れない)。
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from infers.journal import JournalWriter  # noqa: E402

from daily_reconciliation import (  # noqa: E402
    compare, count_journal_exits_for_day, count_mt5_exit_deals_for_day,
)

UTC = dt.timezone.utc


def _write_fsm(journal: JournalWriter, bar_time: dt.datetime, transition: str,
              **payload) -> None:
    journal.set_bar(bar_time)
    journal.record("FSM", {"position_id": "p1", "transition": transition, **payload})


class TestCountJournalExitsForDay:
    def test_counts_exit_transitions_on_target_day(self, tmp_path):
        path = tmp_path / "j.jsonl"
        day = dt.date(2026, 7, 4)
        with JournalWriter(path) as j:
            _write_fsm(j, dt.datetime(2026, 7, 4, 1, tzinfo=UTC), "MARKET_ENTRY")
            _write_fsm(j, dt.datetime(2026, 7, 4, 2, tzinfo=UTC), "TP_CLOSE", level="tp1")
            _write_fsm(j, dt.datetime(2026, 7, 4, 3, tzinfo=UTC), "SL_HIT")
            _write_fsm(j, dt.datetime(2026, 7, 5, 1, tzinfo=UTC), "SL_HIT")  # 翌日は除外

        counts = count_journal_exits_for_day(path, day)
        assert counts == {"TP_CLOSE": 1, "SL_HIT": 1}

    def test_non_exit_transitions_are_excluded(self, tmp_path):
        path = tmp_path / "j.jsonl"
        day = dt.date(2026, 7, 4)
        with JournalWriter(path) as j:
            _write_fsm(j, dt.datetime(2026, 7, 4, 1, tzinfo=UTC), "MARKET_ENTRY")
            _write_fsm(j, dt.datetime(2026, 7, 4, 1, tzinfo=UTC), "MOVE_SL", new_sl=100)

        assert count_journal_exits_for_day(path, day) == {}

    def test_empty_journal_returns_empty_dict(self, tmp_path):
        path = tmp_path / "j.jsonl"
        JournalWriter(path).close()
        assert count_journal_exits_for_day(path, dt.date(2026, 7, 4)) == {}


class TestCompare:
    def test_matching_counts(self):
        result = compare({"SL_HIT": 2, "TP_CLOSE": 1}, 3)
        assert result["match"] is True
        assert result["diff"] == 0
        assert result["journal_exit_total"] == 3

    def test_journal_has_more_exits_than_mt5(self):
        result = compare({"SL_HIT": 3}, 1)
        assert result["match"] is False
        assert result["diff"] == 2

    def test_mt5_has_more_exits_than_journal(self):
        result = compare({}, 2)
        assert result["match"] is False
        assert result["diff"] == -2


class _FakeDeal:
    def __init__(self, symbol: str, entry: int) -> None:
        self.symbol = symbol
        self.entry = entry


class _FakeMT5:
    DEAL_ENTRY_OUT = 1

    def __init__(self, *, deals=None, init_ok: bool = True) -> None:
        self._deals = deals or []
        self._init_ok = init_ok
        self.shutdown_called = False

    def initialize(self, **kwargs):  # noqa: ARG002
        return self._init_ok

    def history_deals_get(self, start, end):  # noqa: ARG002
        return list(self._deals)

    def last_error(self):
        return "fake error"

    def shutdown(self):
        self.shutdown_called = True


class TestCountMt5ExitDealsForDay:
    def test_import_error_reports_unavailable(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "MetaTrader5", None)
        result = count_mt5_exit_deals_for_day(None, symbol="XAUUSD", day=dt.date(2026, 7, 4))
        assert result["available"] is False

    def test_initialize_failure_reports_unavailable(self, monkeypatch):
        fake = _FakeMT5(init_ok=False)
        monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
        result = count_mt5_exit_deals_for_day(None, symbol="XAUUSD", day=dt.date(2026, 7, 4))
        assert result["available"] is False

    def test_counts_only_matching_symbol_exit_deals(self, monkeypatch):
        fake = _FakeMT5(deals=[
            _FakeDeal("XAUUSD", 1), _FakeDeal("XAUUSD", 1), _FakeDeal("XAUUSD", 0),
            _FakeDeal("BTCUSD", 1),
        ])
        monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
        result = count_mt5_exit_deals_for_day(None, symbol="XAUUSD", day=dt.date(2026, 7, 4))
        assert result["available"] is True
        assert result["exit_deals"] == 2
        assert fake.shutdown_called is True

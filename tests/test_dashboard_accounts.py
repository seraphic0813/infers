"""dashboard/accounts.py のテスト(改善プランT12: 口座横断モニタの土台)。

`MetaTrader5` はテスト環境で実インストール済みのため、実際の接続を試みないよう
`sys.modules` にフェイクモジュールを注入して検証する(実MT5には一切触れない)。
"""

from __future__ import annotations

import sys

from infers.dashboard.accounts import account_snapshot


class _FakePosition:
    def __init__(self, type_: int, volume: float) -> None:
        self.type = type_
        self.volume = volume


class _FakeDeal:
    def __init__(self, symbol: str, entry: int, profit: float,
                 swap: float = 0.0, commission: float = 0.0) -> None:
        self.symbol = symbol
        self.entry = entry
        self.profit = profit
        self.swap = swap
        self.commission = commission


class _FakeAccountInfo:
    def __init__(self) -> None:
        self.login = 12345
        self.server = "Demo-Server"
        self.name = "Test Account"
        self.balance = 10000.0
        self.equity = 10050.0
        self.currency = "USD"
        self.leverage = 100


class _FakeMT5:
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    DEAL_ENTRY_OUT = 1

    def __init__(self, *, positions=None, deals=None, account_info=None,
                init_ok: bool = True) -> None:
        self._positions = positions or []
        self._deals = deals or []
        self._account_info = account_info
        self._init_ok = init_ok
        self.shutdown_called = False

    def initialize(self, **kwargs):  # noqa: ARG002
        return self._init_ok

    def account_info(self):
        return self._account_info

    def positions_get(self, symbol=None):  # noqa: ARG002
        return list(self._positions)

    def history_deals_get(self, start, end):  # noqa: ARG002
        return list(self._deals)

    def last_error(self):
        return "fake error"

    def shutdown(self):
        self.shutdown_called = True


def test_import_error_fallback(monkeypatch):
    """MetaTrader5 が import できない環境では connected=False を返す。"""
    monkeypatch.setitem(sys.modules, "MetaTrader5", None)
    result = account_snapshot(None, label="test")
    assert result == {"label": "test", "terminal_path": None,
                      "connected": False, "error": "MetaTrader5 package not available"}


def test_initialize_failure_reports_error(monkeypatch):
    fake = _FakeMT5(init_ok=False)
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    result = account_snapshot(None, label="test")
    assert result["connected"] is False
    assert "fake error" in result["error"]


def test_account_info_none_reports_error(monkeypatch):
    fake = _FakeMT5(init_ok=True, account_info=None)
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    result = account_snapshot(None, label="test")
    assert result["connected"] is False
    assert "account_info" in result["error"]


def test_aggregates_positions_and_today_realized_pnl(monkeypatch):
    fake = _FakeMT5(
        positions=[_FakePosition(0, 2.0), _FakePosition(0, 1.0), _FakePosition(1, 0.5)],
        deals=[_FakeDeal("XAUUSD", 1, 12.34),
              _FakeDeal("XAUUSD", 1, -2.0, swap=-0.5, commission=-0.1)],
        account_info=_FakeAccountInfo(),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    result = account_snapshot(None, label="test")
    assert result["connected"] is True
    assert result["long_lots"] == 3.0
    assert result["short_lots"] == 0.5
    assert result["net_lots"] == 2.5
    assert result["today_realized_usd"] == round(12.34 + (-2.0 - 0.5 - 0.1), 2)
    assert fake.shutdown_called is True


def test_no_positions_or_deals_yields_zeros(monkeypatch):
    fake = _FakeMT5(account_info=_FakeAccountInfo())
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    result = account_snapshot(None, label="test")
    assert result["long_lots"] == 0.0
    assert result["short_lots"] == 0.0
    assert result["net_lots"] == 0.0
    assert result["today_realized_usd"] == 0.0


def test_deals_for_other_symbol_are_excluded(monkeypatch):
    fake = _FakeMT5(
        deals=[_FakeDeal("BTCUSD", 1, 999.0)],
        account_info=_FakeAccountInfo(),
    )
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    result = account_snapshot(None, label="test", symbol="XAUUSD")
    assert result["today_realized_usd"] == 0.0

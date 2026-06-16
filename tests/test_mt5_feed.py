"""MT5Feed.iter_closed の堅牢化 (フェーズ8: 再接続バックオフ + 欠損バックフィル)。

MT5 への実接続は行わず、`feed._mt5` に FakeMt5 を注入して
切断・取りこぼし・回復不能のシナリオを決定論的に検証する。
"""

import threading
from datetime import datetime, timezone

import pytest

from infers.data import mt5_feed
from infers.data.feed import FeedError
from infers.data.models import SymbolSpec, Timeframe

UTC = timezone.utc
GOLD = SymbolSpec(name="XAUUSD", tick_size="0.01", lot_step="0.01", digits=2)
BASE = 1_600_000_000          # 2020-09 (実 now より十分過去 → 全バー確定足)
STEP = int(Timeframe.M5.duration.total_seconds())   # 300s

DISCONNECT = object()


def _bar(i: int) -> dict:
    """連番 i の M5 バー (numpy structured row 相当の dict)。"""
    price = 1000 + i
    return {"time": BASE + i * STEP, "open": float(price), "high": float(price + 1),
            "low": float(price - 1), "close": float(price), "tick_volume": 1}


def _series(n: int) -> list[dict]:
    return [_bar(i) for i in range(n)]


def _open_utc(i: int) -> datetime:
    return datetime.fromtimestamp(BASE + i * STEP, tz=UTC)


class FakeMt5:
    """copy_rates_from_pos の応答を台本で与える MT5 モジュールの代役。"""

    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_H1 = 16385
    TIMEFRAME_H4 = 16388
    TIMEFRAME_D1 = 16408
    TIMEFRAME_W1 = 32769

    def __init__(self, series, windows, *, stop=None, always_disconnect=False):
        self.series = series                 # 範囲バックフィル用の全バー
        self._windows = list(windows)        # ポーリング応答の台本 (rows or DISCONNECT)
        self.stop = stop
        self.always_disconnect = always_disconnect
        self.init_calls = 0
        self.shutdown_calls = 0
        self.range_calls: list[tuple[int, int]] = []

    def copy_rates_from_pos(self, name, tf, start_pos, count):
        if self.always_disconnect:
            return None
        if not self._windows:
            if self.stop is not None:
                self.stop.set()
            return []
        resp = self._windows.pop(0)
        if resp is DISCONNECT:
            return None
        if not self._windows and self.stop is not None:
            self.stop.set()                  # 最後の窓 → 処理後にループ終了
        return resp

    def copy_rates_range(self, name, tf, start_dt, end_dt):
        s, e = int(start_dt.timestamp()), int(end_dt.timestamp())
        self.range_calls.append((s, e))
        return [b for b in self.series if s <= b["time"] < e]

    def last_error(self):
        return (-10004, "fake: no connection")

    def initialize(self, **kw):
        self.init_calls += 1
        return True

    def shutdown(self):
        self.shutdown_calls += 1


def _make_feed(fake, **kw) -> mt5_feed.MT5Feed:
    feed = mt5_feed.MT5Feed(poll_interval_s=0.0, reconnect_base_s=0.0,
                            reconnect_max_s=0.0, **kw)
    feed._mt5 = fake                          # connect() を回避して直接注入
    return feed


def _drain(feed, stop) -> list:
    return list(feed.iter_closed(GOLD, Timeframe.M5, stop=stop))


class TestStreaming:
    def test_yields_closed_window_in_order(self):
        stop = threading.Event()
        fake = FakeMt5(_series(3), [[_bar(0), _bar(1), _bar(2)]], stop=stop)
        feed = _make_feed(fake)
        candles = _drain(feed, stop)
        assert [c.open_time for c in candles] == [_open_utc(i) for i in range(3)]
        assert all(c.is_closed for c in candles)

    def test_does_not_yield_forming_bar(self, monkeypatch):
        """窓の最新足がまだ確定していなければ流さない (確定足主義: 第2条)。"""
        stop = threading.Event()
        fake = FakeMt5(_series(3), [[_bar(0), _bar(1), _bar(2)]], stop=stop)
        feed = _make_feed(fake)
        # now を bar2 の途中に固定 → bar0/bar1 のみ確定、bar2 は形成中
        now = _open_utc(2).replace(microsecond=0) + (Timeframe.M5.duration / 3)
        monkeypatch.setattr(mt5_feed, "utc_now", lambda: now)
        candles = _drain(feed, stop)
        assert [c.open_time for c in candles] == [_open_utc(0), _open_utc(1)]


class TestGapBackfill:
    def test_backfills_missing_bars_after_falling_behind(self):
        """窓 (直近3本) が last_open の直後に届かない → 間の実バーを補完する。"""
        stop = threading.Event()
        fake = FakeMt5(_series(11),
                       [[_bar(0), _bar(1), _bar(2)], [_bar(8), _bar(9), _bar(10)]],
                       stop=stop)
        feed = _make_feed(fake)
        candles = _drain(feed, stop)
        # 取りこぼしなく 0..10 が連続して流れる (3..7 はバックフィル由来)
        assert [c.open_time for c in candles] == [_open_utc(i) for i in range(11)]
        assert fake.range_calls, "欠損区間の get_history が呼ばれていない"

    def test_no_backfill_when_contiguous(self):
        """窓が連続していればバックフィルは発生しない。"""
        stop = threading.Event()
        fake = FakeMt5(_series(5),
                       [[_bar(0), _bar(1), _bar(2)], [_bar(2), _bar(3), _bar(4)]],
                       stop=stop)
        feed = _make_feed(fake)
        candles = _drain(feed, stop)
        assert [c.open_time for c in candles] == [_open_utc(i) for i in range(5)]
        assert fake.range_calls == []          # 連続 → range取得なし


class _Tick:
    def __init__(self, t):
        self.time = t


class TestOffsetAutoDetect:
    """サーバー時刻オフセットの初回実測 (本番接続バグ修正: UTC+3 等)。"""

    def test_detects_offset_from_tick(self, monkeypatch):
        from datetime import datetime, timedelta

        now = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)
        monkeypatch.setattr(mt5_feed, "utc_now", lambda: now)
        fake = FakeMt5(_series(0), [])
        # サーバー時刻 = UTC+3 (tick.time が3時間先)
        fake.symbol_info_tick = lambda name: _Tick(int(now.timestamp()) + 3 * 3600)
        feed = _make_feed(fake)                 # auto_detect_offset 既定 True
        feed._ensure_offset(GOLD)
        assert feed._offset == timedelta(hours=3)
        # 2回目は再検出しない
        fake.symbol_info_tick = lambda name: _Tick(int(now.timestamp()) + 9 * 3600)
        feed._ensure_offset(GOLD)
        assert feed._offset == timedelta(hours=3)

    def test_explicit_offset_disables_autodetect(self):
        from datetime import timedelta

        fake = FakeMt5(_series(0), [])
        fake.symbol_info_tick = lambda name: _Tick(0)
        feed = _make_feed(fake, server_utc_offset=timedelta(hours=2))
        feed._ensure_offset(GOLD)
        assert feed._offset == timedelta(hours=2)

    def test_missing_tick_api_falls_back_to_zero(self):
        from datetime import timedelta

        fake = FakeMt5(_series(0), [])          # symbol_info_tick を持たない
        feed = _make_feed(fake)
        feed._ensure_offset(GOLD)               # 例外を出さず 0 へフォールバック
        assert feed._offset == timedelta(0)


class TestReconnect:
    def test_recovers_from_transient_disconnect(self):
        """copy_rates が一度 None (切断) → 再初期化して回復し、以降を流す。"""
        stop = threading.Event()
        fake = FakeMt5(_series(3), [DISCONNECT, [_bar(0), _bar(1), _bar(2)]], stop=stop)
        feed = _make_feed(fake)
        candles = _drain(feed, stop)
        assert [c.open_time for c in candles] == [_open_utc(i) for i in range(3)]
        assert fake.init_calls == 1            # 1回だけ再接続した
        assert fake.shutdown_calls == 1

    def test_gives_up_after_max_attempts(self):
        """回復不能 (常時切断) は試行上限超過で FeedError を上位へ送出する。"""
        fake = FakeMt5(_series(0), [], always_disconnect=True)
        feed = _make_feed(fake, max_reconnect_attempts=2)
        with pytest.raises(FeedError, match="unrecoverable"):
            next(feed.iter_closed(GOLD, Timeframe.M5))
        assert fake.init_calls == 2            # 上限ぶん再接続を試みた

    def test_stop_during_reconnect_terminates_cleanly(self):
        """再接続待機中に stop されたら速やかに終了する (例外なし)。"""
        stop = threading.Event()
        stop.set()                             # 最初から停止指示
        fake = FakeMt5(_series(0), [], always_disconnect=True)
        feed = _make_feed(fake)
        assert _drain(feed, stop) == []

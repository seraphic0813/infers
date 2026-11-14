"""ライブ層の堅牢化 (2026-07 デモ稼働の異常2件の回帰防止)。

異常1: GO 直後にプロセスが終了していた。
  - 指値の成功コード 10008 (PLACED) を失敗扱いしていた
  - 注文コメントが MT5 上限 31 文字を超えていた
  - 発注失敗が TradingLoop → LiveRunner → run_live を素通りしてプロセスを落とした
異常2: 確定足が止まってもハートビートしか出ず、停止を検知できなかった。
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from infers.execution.mt5_adapter import (
    MT5_COMMENT_MAX, MT5LiveBroker, OrderRejected, order_comment, pid_key)
from infers.journal import JournalWriter, read_journal

from tests.test_integration import (
    GOLD, FakeClient, make_runner_with_risk)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# 注文コメント
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pid,oid", [
    ("XAUUSD-2026-07-10T01:05:00+00:00-L", "XAUUSD-2026-07-10T01:05:00+00:00-L/001/mkt_entry"),
    ("XAUUSD/M5/2026-07-02T01:20:00+00:00", "XAUUSD/M5/2026-07-02T01:20:00+00:00/001/probe_limit"),
    ("p", "other-format-order-id"),
])
def test_order_comment_fits_mt5_limit_and_is_deterministic(pid, oid):
    c = order_comment(oid, pid)
    assert len(c) <= MT5_COMMENT_MAX
    assert c == order_comment(oid, pid)
    assert c.split("/", 1)[0] == pid_key(pid)


def test_probe_limit_tag_survives_shortening():
    pid = "XAUUSD/M5/2026-07-02T01:20:00+00:00"
    assert "/probe_limit" in order_comment(f"{pid}/001/probe_limit", pid)


# ---------------------------------------------------------------------------
# MT5LiveBroker の成功コード判定 (偽 MetaTrader5 モジュール)
# ---------------------------------------------------------------------------

class _FakeMt5Trade:
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_SLTP = 6
    TRADE_ACTION_REMOVE = 8
    TRADE_RETCODE_PLACED = 10008
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010
    TRADE_RETCODE_NO_CHANGES = 10025
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TIME_GTC = 0

    def __init__(self, retcode):
        self.retcode = retcode
        self.requests: list[dict] = []

    def order_send(self, req):
        self.requests.append(req)
        return SimpleNamespace(retcode=self.retcode, order=777, price=2000.0)

    def last_error(self):
        return (1, "Success")


def _broker(retcode) -> tuple[MT5LiveBroker, _FakeMt5Trade]:
    b = MT5LiveBroker(GOLD, filling_mode=1)
    fake = _FakeMt5Trade(retcode)
    b._mt5 = fake
    return b, fake


def test_pending_order_placed_code_is_success():
    """回帰: 指値の正規の成功 10008 で FeedError を出してプロセスが落ちていた。"""
    b, fake = _broker(10008)
    pid = "XAUUSD/M5/2026-07-02T01:20:00+00:00"
    b.place_limit(client_order_id=f"{pid}/001/probe_limit", position_id=pid,
                  direction=-1, price_int=200_000, volume_steps=1, sl_int=201_000)
    assert len(fake.requests[0]["comment"]) <= MT5_COMMENT_MAX
    assert b._pid_from_comment(fake.requests[0]["comment"]) == pid


def test_market_order_rejected_raises_order_rejected_with_context():
    b, _ = _broker(10018)                       # MARKET_CLOSED
    with pytest.raises(OrderRejected) as ei:
        b.place_market(client_order_id="P/001/mkt_entry", position_id="P",
                       direction=1, volume_steps=1, sl_int=190_000)
    e = ei.value
    assert (e.op, e.position_id, e.retcode) == ("place_market", "P", 10018)


def test_market_order_placed_code_is_not_success():
    """成行の成功は DONE のみ (PLACED は約定していない)。"""
    b, _ = _broker(10008)
    with pytest.raises(OrderRejected):
        b.place_market(client_order_id="P/001/mkt_entry", position_id="P",
                       direction=1, volume_steps=1, sl_int=190_000)


# ---------------------------------------------------------------------------
# LiveRunner: 発注失敗でプロセスを落とさない
# ---------------------------------------------------------------------------

def test_entry_rejection_is_journaled_and_loop_continues(tmp_path):
    journal = JournalWriter(tmp_path / "j.jsonl")
    runner, broker, risk = make_runner_with_risk(FakeClient(), journal=journal)

    def _reject(**kw):
        raise OrderRejected(op="place_limit", client_order_id=kw["client_order_id"],
                            position_id=kw["position_id"], retcode=10018,
                            last_error=(1, "x"), comment=None)

    broker.place_limit = _reject
    bars = runner.run(max_bars=4)               # 例外で落ちずに最後まで回る
    journal.close()
    kinds = [e.kind for e in read_journal(tmp_path / "j.jsonl")]
    assert bars == 4
    assert "ORDER_REJECT" in kinds
    assert not risk.kill_switch_engaged           # 新規の見送りでは新規停止しない
    assert runner.loop.open_positions == {}       # 失敗した建玉は登録されない


def test_defensive_order_failure_engages_kill_switch(tmp_path):
    journal = JournalWriter(tmp_path / "j.jsonl")
    runner, _broker_, risk = make_runner_with_risk(FakeClient(), journal=journal)

    def _fail():
        raise OrderRejected(op="close_volume", client_order_id="P/002/tp1_half",
                            position_id="P", retcode=10006, last_error=(1, "x"),
                            comment=None)

    runner._guarded(_fail)
    journal.close()
    kinds = [e.kind for e in read_journal(tmp_path / "j.jsonl")]
    assert "ORDER_ERROR" in kinds
    assert risk.kill_switch_engaged


# ---------------------------------------------------------------------------
# フィード停止の検知 (FEED_STALE)
# ---------------------------------------------------------------------------

def _stale_runner(tmp_path):
    journal = JournalWriter(tmp_path / "hb.jsonl")
    runner, _b, _r = make_runner_with_risk(FakeClient(), journal=journal)
    runner._feed = SimpleNamespace(last_activity_utc=None, offset_pending=False)
    return runner, journal


def test_feed_stale_recorded_once_when_market_moves_but_bars_stop(tmp_path):
    runner, journal = _stale_runner(tmp_path)
    t0 = datetime(2026, 7, 13, 1, 0, tzinfo=UTC)
    runner._last_bar_close = t0
    for minutes in (20, 25, 30):
        now = t0 + timedelta(minutes=minutes)
        runner._feed.last_activity_utc = now - timedelta(seconds=5)   # 価格は動いている
        runner._heartbeat_tick(now)
    journal.close()
    recs = list(read_journal(tmp_path / "hb.jsonl"))
    assert [e.kind for e in recs].count("FEED_STALE") == 1
    hb = [e for e in recs if e.kind == "HEARTBEAT"][-1]
    assert hb.data["last_bar_close"] == t0.isoformat() and hb.data["lag_s"] == 1800


def test_no_feed_stale_while_market_is_closed(tmp_path):
    runner, journal = _stale_runner(tmp_path)
    t0 = datetime(2026, 7, 11, 1, 0, tzinfo=UTC)           # 土曜
    runner._last_bar_close = t0
    runner._feed.last_activity_utc = t0                    # 以後価格は動かない
    runner._heartbeat_tick(t0 + timedelta(hours=10))
    journal.close()
    assert "FEED_STALE" not in [e.kind for e in read_journal(tmp_path / "hb.jsonl")]

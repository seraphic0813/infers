"""監視ダッシュボード (src/infers/dashboard) のテスト。

MT5 へは一切接続せず、FakeFeed / LedgerBroker を注入する runner_factory で
LiveController のライフサイクルを検証する。資格情報がジャーナル・ステータス
応答・標準出力に漏れないことを明示的に担保する (CLAUDE.md 安全原則)。
"""

import json
import threading
import urllib.error
import urllib.request
from decimal import Decimal

import pytest

from infers.ai.gateway import AiGateway, VerdictCache
from infers.core.loop import ProviderOutput
from infers.core.models import Timeframe
from infers.execution.mt5_adapter import LiveRunner
from infers.execution.risk import RiskConfig, RiskManager
from infers.execution.sm import FsmConfig
from infers.backtest.engine import LedgerBroker

from infers.dashboard import monitor
from infers.dashboard.controller import LiveController
from infers.dashboard.server import make_server

from tests.test_integration import (
    CANDLES, POLICY, SCRIPT, FakeClient, FakeFeed, GOLD, ScriptedProvider,
)


# ---------------------------------------------------------------------------
# テスト用 factory / フィード
# ---------------------------------------------------------------------------

def _build_runner(journal):
    broker = LedgerBroker(spread_ticks=2, min_stop_distance_ticks=5)
    feed = FakeFeed(CANDLES)
    runner = LiveRunner(
        feed=feed, spec=GOLD, tf=Timeframe.M5, broker=broker,
        provider=ScriptedProvider(SCRIPT),
        gateway=AiGateway(client=FakeClient(), cache=VerdictCache(), policy=POLICY),
        risk=RiskManager(RiskConfig(max_position_volume_steps=4, max_total_volume_steps=8,
                                    max_spread_ticks=10, daily_loss_limit_tick_steps=10_000)),
        fsm_config=FsmConfig(min_be_distance_ticks=10, be_offset_ticks=2,
                             breakout_buffer_ticks=10),
        event_source=broker.process_bar, spread_fn=lambda: 2,
        journal=journal,
    )
    return runner, feed, broker


class _BlockingFeed(FakeFeed):
    """stop がセットされるまでブロックし続けるフィード (常駐稼働の模擬)。"""

    def __init__(self):
        super().__init__([])

    def iter_closed(self, spec, tf, *, stop=None):
        # 確定足を一切流さず stop 待ち。常駐を模擬する
        while stop is None or not stop.wait(0.01):
            if stop is not None and stop.is_set():
                return
        return
        yield  # pragma: no cover - generator 化のため


def _make_controller(holder=None, *, blocking=False, chdir_tmp=None):
    def factory(*, args, login, password, server, journal, warmup_days=0):
        if holder is not None:
            holder["creds"] = (login, password, server)
        if blocking:
            feed = _BlockingFeed()

            class _Idle:
                loop = type("L", (), {"close_all_open": staticmethod(lambda r: [])})()

                def __init__(self, feed):
                    self._feed = feed

                def run(self, *, stop=None, max_bars=None):
                    for _ in self._feed.iter_closed(None, None, stop=stop):
                        pass
                    return 0

                def shutdown(self, reason="SHUTDOWN"):
                    return []

            return _Idle(feed), feed
        runner, feed, broker = _build_runner(journal)
        if holder is not None:
            holder["broker"] = broker
        return runner, feed

    return LiveController(runner_factory=factory)


# ---------------------------------------------------------------------------
# controller ライフサイクル
# ---------------------------------------------------------------------------

class TestController:
    def test_full_lifecycle_runs_and_shuts_down(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        holder: dict = {}
        ctrl = _make_controller(holder)
        ctrl.start(symbol="XAUUSD", login=785662, password="secret-pw",
                   server="VantageTradingLtd-Demo")
        # FakeFeed は CANDLES を流し切って完了する
        for _ in range(200):
            if not ctrl.running:
                break
            threading.Event().wait(0.01)
        assert not ctrl.running
        # 全行程を経てポジションはクローズ済み (既存 shutdown 経路)
        assert holder["broker"].position("live1") is None
        # 資格情報は factory へ届いている
        assert holder["creds"] == (785662, "secret-pw", "VantageTradingLtd-Demo")
        # 処理バー数は set_bar 呼び出し (全確定足) を数えた値 = CANDLES 本数
        assert ctrl.status()["bars_processed"] == len(CANDLES)

    def test_double_start_rejected(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ctrl = _make_controller(blocking=True)
        ctrl.start(symbol="XAUUSD", login=1, password="p", server="s")
        try:
            assert ctrl.running
            with pytest.raises(RuntimeError):
                ctrl.start(symbol="XAUUSD", login=1, password="p", server="s")
        finally:
            ctrl.stop()
        assert not ctrl.running

    def test_status_never_exposes_credentials(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ctrl = _make_controller(blocking=True)
        ctrl.start(symbol="XAUUSD", login=785662, password="topsecret",
                   server="VantageTradingLtd-Demo")
        try:
            st = ctrl.status()
            blob = json.dumps(st, default=str)
            assert "topsecret" not in blob
            assert "785662" not in blob
            assert "password" not in st
        finally:
            ctrl.stop()

    def test_journal_records_no_credentials(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ctrl = _make_controller()
        ctrl.start(symbol="XAUUSD", login=785662, password="topsecret", server="srv")
        for _ in range(200):
            if not ctrl.running:
                break
            threading.Event().wait(0.01)
        text = (tmp_path / ctrl.status()["journal_path"]).read_text(encoding="utf-8")
        assert "topsecret" not in text
        assert "785662" not in text


# ---------------------------------------------------------------------------
# monitor 要約
# ---------------------------------------------------------------------------

class TestWarmupProvider:
    """ウォームアップ: 過去足を inner に流し、ライブで重複足をスキップする。"""

    def test_warmup_feeds_history_then_skips_duplicates(self):
        from infers.dashboard.controller import _WarmupProvider
        from tests.test_integration import mk_candle

        class _StubInner:
            def __init__(self):
                self.seen = []

            def on_candle(self, candle):
                self.seen.append(candle.open_time)
                return ProviderOutput()

        hist = [mk_candle(i, 1005, 995, 1000) for i in range(5)]
        feed = FakeFeed(hist)
        inner = _StubInner()
        wp = _WarmupProvider(inner)
        # get_history は [start, end) を返す。十分広い days で全件取得
        n = wp.warmup(feed, GOLD, Timeframe.M5, days=3650)
        assert n == 5
        assert len(inner.seen) == 5
        assert wp.warmup_bars == 5

        # ライブ: ウォームアップ済みの足 (i=4) はスキップ、新しい足 (i=5) は通す
        dup = hist[4]
        out_dup = wp.on_candle(dup)
        assert isinstance(out_dup, ProviderOutput)
        assert len(inner.seen) == 5            # 重複は inner へ渡さない

        nxt = mk_candle(5, 1005, 995, 1000)
        wp.on_candle(nxt)
        assert inner.seen[-1] == nxt.open_time  # 新しい足は委譲された

    def test_warmup_calls_reset_position_mirror_hook(self):
        """自己ミラー式プロバイダ (例: smc_bos) は warmup() 完了直後に
        reset_position_mirror が呼ばれ、発注なしのウォームアップだけで
        「建玉中」のまま固着しない (ファントム建玉バグの回帰防止)。"""
        from infers.dashboard.controller import _WarmupProvider
        from tests.test_integration import mk_candle

        class _StubInnerWithMirror:
            def __init__(self):
                self.seen = []
                self.reset_calls = 0

            def on_candle(self, candle):
                self.seen.append(candle.open_time)
                return ProviderOutput()

            def reset_position_mirror(self):
                self.reset_calls += 1

        hist = [mk_candle(i, 1005, 995, 1000) for i in range(5)]
        feed = FakeFeed(hist)
        inner = _StubInnerWithMirror()
        wp = _WarmupProvider(inner)
        wp.warmup(feed, GOLD, Timeframe.M5, days=3650)
        assert inner.reset_calls == 1

    def test_warmup_days_zero_is_noop(self):
        from infers.dashboard.controller import _WarmupProvider
        from tests.test_integration import mk_candle

        feed = FakeFeed([mk_candle(i, 1005, 995, 1000) for i in range(3)])

        class _StubInner:
            def __init__(self):
                self.n = 0

            def on_candle(self, candle):
                self.n += 1
                return ProviderOutput()

        inner = _StubInner()
        wp = _WarmupProvider(inner)
        assert wp.warmup(feed, GOLD, Timeframe.M5, days=0) == 0
        assert inner.n == 0


class TestMonitor:
    def test_summarize_synthetic_journal(self, tmp_path):
        from infers.journal import JournalWriter
        from datetime import datetime, timezone

        path = tmp_path / "XAUUSD_test.jsonl"
        jw = JournalWriter(path)
        jw.record("SESSION", {"mode": "live", "symbol": "XAUUSD", "ai_client": "rule"})
        jw.set_bar(datetime(2026, 6, 16, tzinfo=timezone.utc))
        jw.record("VERDICT", {"decision": "GO", "source": "L1"})
        jw.record("FSM", {"position_id": "p1", "transition": "PLACE_PROBE"})
        jw.record("FSM", {"position_id": "p1", "transition": "PROBE_FILL"})
        jw.close()

        s = monitor.summarize(path)
        assert s["exists"] is True
        assert s["counts"]["FSM"] == 2
        assert s["positions"]["p1"] == ["PLACE_PROBE", "PROBE_FILL"]
        assert s["bars"] == 1
        assert s["ai_client"] == "rule"
        assert s["golden"]["ok"] is True

    def test_summarize_missing_file(self, tmp_path):
        s = monitor.summarize(tmp_path / "nope.jsonl")
        assert s["exists"] is False
        assert s["bars"] == 0


# ---------------------------------------------------------------------------
# HTTP サーバ (ephemeral port)
# ---------------------------------------------------------------------------

class TestServer:
    @pytest.fixture
    def server(self):
        httpd, token = make_server(port=0, controller=_make_controller(blocking=True))
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        host, port = httpd.server_address
        base = f"http://127.0.0.1:{port}"
        yield base, token, httpd
        httpd.shutdown()

    def test_index_served(self, server):
        base, _token, _ = server
        with urllib.request.urlopen(base + "/") as r:
            body = r.read().decode("utf-8")
        assert r.status == 200
        assert "INFERS" in body

    def test_status_ok(self, server):
        base, _token, _ = server
        with urllib.request.urlopen(base + "/api/status") as r:
            data = json.loads(r.read())
        assert r.status == 200
        assert data["running"] is False

    def test_post_without_token_is_401(self, server):
        base, _token, _ = server
        req = urllib.request.Request(base + "/api/stop", method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req)
        assert exc.value.code == 401

    def test_journal_uses_running_session_file(self, tmp_path, monkeypatch):
        """稼働中は今日(UTC)の日付ではなくセッションの実ファイルを参照する
        (UTC 日付跨ぎで監視が空になるロールオーバーバグの回帰防止)。"""
        monkeypatch.chdir(tmp_path)
        ctrl = _make_controller(blocking=True)
        httpd, _token = make_server(port=0, controller=ctrl)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            ctrl.start(symbol="XAUUSD", login=1, password="p", server="s")
            for _ in range(300):                  # journal_path が立つまで待つ
                if ctrl.status().get("journal_path"):
                    break
                threading.Event().wait(0.01)
            port = httpd.server_address[1]
            j = json.loads(
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/journal").read())
            # セッションの実ファイル (SESSION 記録済み) が読めている
            assert j["exists"] is True
            assert j["counts"].get("SESSION", 0) >= 1
        finally:
            ctrl.stop()
            httpd.shutdown()

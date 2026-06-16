"""LiveController — LiveRunner を worker スレッドで起動/安全停止する操作層。

設計上の境界 (CLAUDE.md / プラン):
  - 既存ソース (main.py / mt5_adapter.py / mt5_feed.py 等) は **無改変**。
    本クラスは既存の公開要素を import して呼ぶだけ。
  - サブプロセスではなく **同一プロセス内スレッド + stop Event** を使う。
    `LiveRunner.run(stop=...)` / `MT5Feed.iter_closed(stop=...)` が既に
    stop Event を完備しているため、停止は Event.set() で安全に行える。
    停止時は既存の `runner.shutdown()` (未約定取消+手仕舞い) を必ず通す。
  - 口座資格情報 (login/password/server) は factory へ渡すのみで
    インスタンスに保持しない。status() にも一切含めない (CLAUDE.md 安全原則)。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from infers.dashboard import monitor


class _WarmupProvider:
    """SignalProvider をラップし、起動前に過去足を流してインジケーターを育てる。

    ウォームアップ: feed.get_history で取得した確定足を inner.on_candle に順次
    流し込み、戻り値は破棄する (発注はせず内部状態=SMA/RSI/ダウ等だけ育てる)。
    ライブ移行後: ウォームアップ済みの時刻以前の足はスキップして二重処理を防ぐ
    (同一足の再処理で provider 内部の時系列前提が壊れるのを防ぐ。確定足主義は維持)。
    既存コアは無改変 — SignalProvider プロトコル (on_candle) を委譲する。
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self._last_open = None
        self.warmup_bars = 0

    def warmup(self, feed, spec, tf, *, days: int, stop=None) -> int:
        """[now-days, now) の確定足を流し込む。処理本数を返す。

        stop (threading.Event) がセットされたら途中で中断する (約5分かかるため
        その間の「安全停止」を効かせる)。warmup_bars は逐次更新され status が読む。
        """
        if days <= 0:
            return 0
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        for candle in feed.get_history(spec, tf, start, end):
            if stop is not None and stop.is_set():
                break
            if self._last_open is not None and candle.open_time <= self._last_open:
                continue
            self._inner.on_candle(candle)        # 戻り値は破棄 (発注しない)
            self._last_open = candle.open_time
            self.warmup_bars += 1
        return self.warmup_bars

    def on_candle(self, candle):
        from infers.core.loop import ProviderOutput
        if self._last_open is not None and candle.open_time <= self._last_open:
            return ProviderOutput()              # ウォームアップ済み → スキップ
        self._last_open = candle.open_time
        return self._inner.on_candle(candle)


class _CountingJournal:
    """JournalWriter をラップし、処理した確定足数 (set_bar 呼び出し) を数える。

    loop は全確定足で set_bar() を呼ぶ (エントリー候補が無いウォームアップ中も)。
    一方 record() は判断が出た時のみ。よってジャーナル追記行ではなく set_bar の
    一意な bar_time を数えることで「処理バー数 (鼓動)」を正確に把握する。
    既存コアは無改変 — JournalSink プロトコル (set_bar/record/fsm_sink) を委譲する。
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self._bars: set = set()
        self._last_bar = None

    # JournalSink 委譲
    def set_bar(self, bar_time) -> None:
        self._bars.add(bar_time)
        self._last_bar = bar_time
        self._inner.set_bar(bar_time)

    def record(self, kind: str, data: dict) -> None:
        self._inner.record(kind, data)

    def fsm_sink(self, position_id: str):
        return self._inner.fsm_sink(position_id)

    def close(self) -> None:
        self._inner.close()

    @property
    def path(self):
        return self._inner.path

    @property
    def bars_processed(self) -> int:
        return len(self._bars)

    @property
    def last_bar(self):
        return self._last_bar


def _v10_args(symbol: str):
    """v1.0 確定構成の Namespace を既存 parse_args から生成する。

    rule_depth50 ベースライン: --macro-wave2 --depth-screen --depth-max 0.50 --no-fib-score。
    UI からの逸脱を防ぐため固定する。
    """
    from infers.main import parse_args

    return parse_args([
        "--mode", "live", "--symbol", symbol,
        "--macro-wave2", "--depth-screen", "--depth-max", "0.50", "--no-fib-score",
    ])


def _default_runner_factory(*, args, login, password, server, journal):
    """本番用 factory: 既存要素を import して LiveRunner を組む (構築のみ・無改変)。

    provider は _WarmupProvider で包むだけ (ウォームアップ自体は controller が
    stop 割り込み・進捗管理付きで実行する)。
    """
    from infers.data.models import Timeframe
    from infers.data.mt5_feed import MT5Feed
    from infers.execution.mt5_adapter import LiveRunner, MT5LiveBroker
    from infers.execution.risk import RiskManager
    from infers.main import (
        DEFAULT_FSM, DEFAULT_RISK, SYMBOLS, _build_gateway, build_provider,
    )

    spec = SYMBOLS[args.symbol]
    tf = Timeframe(args.tf)
    # サーバー時刻オフセット (Vantage は UTC+3 等) は MT5Feed が初回ポーリングで
    # 自動検出する (本体修正)。ここでは明示指定しない。
    feed = MT5Feed(login=login, password=password, server=server)
    broker = MT5LiveBroker(spec)
    feed.connect()
    broker.connect()
    provider = _WarmupProvider(build_provider(args))   # ウォームアップは controller が実行
    runner = LiveRunner(
        feed=feed, spec=spec, tf=tf, broker=broker,
        provider=provider,
        gateway=_build_gateway(args, cache_only=False),
        risk=RiskManager(DEFAULT_RISK), fsm_config=DEFAULT_FSM,
        journal=journal,
    )
    return runner, feed


class LiveController:
    """ライブ稼働のライフサイクル (起動/安全停止/状態照会) を管理する。

    runner_factory を差し替えることで MT5 非依存にテストできる
    (factory(*, args, login, password, server, journal) -> (runner, feed))。
    """

    def __init__(self, *, runner_factory: Callable[..., Any] | None = None) -> None:
        self._factory = runner_factory or _default_runner_factory
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._runner: Any = None
        self._feed: Any = None
        self._journal: Any = None
        # 公開してよいメタ情報のみ保持 (資格情報は保持しない)
        self._symbol: str | None = None
        self._started_at: str | None = None
        self._journal_path: str | None = None
        self._last_error: str | None = None
        self._stopped_reason: str | None = None
        self._phase: str | None = None           # warmup / live / None(停止)

    # -- 操作 ---------------------------------------------------------------

    def start(self, *, symbol: str, login: int | None,
              password: str | None, server: str | None,
              warmup_days: int = 0) -> None:
        """稼働を開始する。二重起動は弾く。

        runner/feed/journal の構築 (SQLite VerdictCache 等スレッド affinity の
        ある資源を含む) は **worker スレッド内** で行う。SQLite は生成スレッドと
        使用スレッドが一致しないと ProgrammingError になるため、メインスレッドで
        作って worker で使う構成は不可。資格情報も self に保持せずスレッド引数で渡す。
        warmup_days>0 で起動時に過去足を流しインジケーターを育てる。
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("already running")
            self._runner = None
            self._feed = None
            self._journal = None
            self._symbol = symbol
            self._journal_path = None
            self._started_at = datetime.now(timezone.utc).isoformat()
            self._last_error = None
            self._stopped_reason = None
            self._phase = "warmup" if warmup_days > 0 else "live"
            self._stop = threading.Event()
            self._thread = threading.Thread(
                target=self._run, name="infers-live", daemon=True,
                args=(symbol, login, password, server, warmup_days))
            self._thread.start()

    def _run(self, symbol, login, password, server, warmup_days) -> None:
        """worker スレッド本体。構築〜稼働〜安全停止までを同一スレッドで行う。"""
        from infers.main import _open_live_journal

        reason = "completed"
        try:
            args = _v10_args(symbol)
            # set_bar 呼び出しを数えるラッパで包む (真の処理バー数=鼓動)
            self._journal = _CountingJournal(_open_live_journal(args))
            self._journal_path = str(self._journal.path)
            self._runner, self._feed = self._factory(
                args=args, login=login, password=password, server=server,
                journal=self._journal,
            )
            # ウォームアップ (stop 割り込み可・進捗は status の warmup_bars に逐次反映)。
            provider = getattr(self._runner, "_provider", None)
            if warmup_days > 0 and hasattr(provider, "warmup"):
                from infers.data.models import Timeframe
                from infers.main import SYMBOLS
                provider.warmup(self._feed, SYMBOLS[symbol], Timeframe(args.tf),
                                days=warmup_days, stop=self._stop)
            self._phase = "live"                 # ウォームアップ完了 → 生中継へ
            if not self._stop.is_set():
                self._runner.run(stop=self._stop)
        except Exception as exc:                 # noqa: BLE001 — UI へ理由を伝える
            self._last_error = repr(exc)
            reason = "error"
        finally:
            if self._runner is not None:
                try:
                    self._runner.shutdown()      # 既存の安全停止: 未約定取消+手仕舞い
                except Exception as exc:          # noqa: BLE001
                    self._last_error = (self._last_error or "") + f" | shutdown: {exc!r}"
            if self._feed is not None:
                try:
                    self._feed.close()
                except Exception:                 # noqa: BLE001
                    pass
            if self._journal is not None:
                try:
                    self._journal.close()
                except Exception:                 # noqa: BLE001
                    pass
            if self._stop.is_set() and reason == "completed":
                reason = "stopped"
            self._stopped_reason = reason
            self._phase = None

    def stop(self, *, timeout: float = 30.0) -> None:
        """安全停止を要求する (Event.set → スレッド join)。"""
        thread = self._thread
        if thread is None or not thread.is_alive():
            return
        self._stop.set()
        thread.join(timeout=timeout)

    # -- 照会 ---------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        """資格情報を含まない状態スナップショット。"""
        # 処理バー数は set_bar 呼び出し (鼓動) を数えたラッパから取得。
        # 稼働終了後 (journal close 済み) はジャーナル追記イベントから補完。
        if self._journal is not None and hasattr(self._journal, "bars_processed"):
            bars = self._journal.bars_processed
        elif self._journal_path:
            bars = monitor.count_bars(self._journal_path)
        else:
            bars = 0
        offset = getattr(self._feed, "_offset", None)
        offset_h = (offset.total_seconds() / 3600.0) if offset is not None else None
        last_bar = getattr(self._journal, "last_bar", None)
        provider = getattr(self._runner, "_provider", None)
        warmup_bars = getattr(provider, "warmup_bars", None)
        return {
            "running": self.running,
            "phase": self._phase,
            "symbol": self._symbol,
            "started_at": self._started_at,
            "journal_path": self._journal_path,
            "bars_processed": bars,
            "warmup_bars": warmup_bars,
            "last_bar_time": last_bar.isoformat() if last_bar else None,
            "server_utc_offset_h": offset_h,
            "last_error": self._last_error,
            "stopped_reason": self._stopped_reason,
        }

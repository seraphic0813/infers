"""MetaTrader5 接続ラッパーの骨組み (設計書 §2 / §9)。

float が許される唯一の場所 = MT5 API 境界。受信した瞬間に
SymbolSpec.float_to_ticks() で整数ティックへ変換する (CLAUDE.md 第6条)。

MetaTrader5 パッケージは Windows 専用のため import は connect() まで遅延し、
バックテスト/CI 環境では本モジュールを読み込んでもエラーにならない。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from infers.data.feed import FeedError, MarketFeed, ensure_utc
from infers.core.models import Candle, SymbolSpec, Timeframe, utc_now


class MT5Feed(MarketFeed):
    """MetaTrader5 ターミナル経由のフィード実装(骨組み)。

    Vantage Trading / Swift Trader はいずれも MT5 ターミナルを介して接続する。
    ターミナルの起動・ログイン情報は config/broker.yaml から渡される想定。
    """

    def __init__(
        self,
        *,
        server_utc_offset: timedelta | None = None,
        auto_detect_offset: bool = True,
        poll_interval_s: float = 1.0,
        terminal_path: str | None = None,
        login: int | None = None,
        password: str | None = None,   # 環境変数から注入。ログ出力禁止 (CLAUDE.md 開発環境)
        server: str | None = None,
        poll_lookback: int = 3,        # 1ポーリングで遡って取得する本数 (形成足含む)
        reconnect_base_s: float = 1.0,     # 再接続バックオフの初期待機 (指数増)
        reconnect_max_s: float = 60.0,     # 再接続バックオフの上限待機
        max_reconnect_attempts: int = 10,  # 連続再接続の試行上限 (超過で FeedError)。0=無制限
    ) -> None:
        # server_utc_offset: ブローカーサーバー時刻 − UTC。MT5 の rates['time'] は
        # サーバー時刻基準の epoch 秒であり、ここで UTC へ正規化する。
        # 明示指定がなければ初回ポーリング時に symbol_info_tick から実測する
        # (Vantage は UTC+3 等。これを 0 のままにすると確定足が「未来足」と
        #  誤判定され iter_closed が永久に何も流さない: フェーズ8 本番接続バグ)。
        self._offset = server_utc_offset if server_utc_offset is not None else timedelta(0)
        self._auto_offset = auto_detect_offset and server_utc_offset is None
        self._offset_detected = False
        self._poll_interval_s = poll_interval_s
        self._poll_lookback = max(1, poll_lookback)
        self._reconnect_base_s = reconnect_base_s
        self._reconnect_max_s = reconnect_max_s
        self._max_reconnect_attempts = max_reconnect_attempts
        self._init_kwargs: dict[str, Any] = {}
        if terminal_path:
            self._init_kwargs["path"] = terminal_path
        if login is not None:
            self._init_kwargs.update(login=login, password=password, server=server)
        self._mt5: Any | None = None  # 遅延 import した MetaTrader5 モジュール
        # 稼働中の再接続成功回数。上位 (LiveRunner) が増加を監視し、復帰直後に
        # リコンサイルを起動するためのフック (フェーズ8 #6)。
        self.reconnect_count = 0

    # -- ライフサイクル -----------------------------------------------------

    def connect(self) -> None:
        if self._mt5 is not None:
            return  # 冪等
        try:
            import MetaTrader5 as mt5  # Windows 専用・遅延 import
        except ImportError as e:
            raise FeedError(
                "MetaTrader5 package not available. "
                "Install with `pip install infers[live]` on Windows."
            ) from e
        if not mt5.initialize(**self._init_kwargs):
            raise FeedError(f"mt5.initialize failed: {mt5.last_error()}")
        self._mt5 = mt5

    def close(self) -> None:
        if self._mt5 is not None:
            self._mt5.shutdown()
            self._mt5 = None

    # -- 内部ヘルパー ---------------------------------------------------------

    def _require_mt5(self) -> Any:
        if self._mt5 is None:
            raise FeedError("not connected — call connect() first")
        return self._mt5

    def _tf_const(self, tf: Timeframe) -> int:
        mt5 = self._require_mt5()
        return {
            Timeframe.M5: mt5.TIMEFRAME_M5,
            Timeframe.M15: mt5.TIMEFRAME_M15,
            Timeframe.H1: mt5.TIMEFRAME_H1,
            Timeframe.H4: mt5.TIMEFRAME_H4,
            Timeframe.D1: mt5.TIMEFRAME_D1,
            Timeframe.W1: mt5.TIMEFRAME_W1,
        }[tf]

    def _server_time_to_utc(self, epoch_s: int) -> datetime:
        """MT5 の rates['time'] (サーバー時刻基準 epoch 秒) → tz-aware UTC。"""
        return datetime.fromtimestamp(int(epoch_s), tz=timezone.utc) - self._offset

    def _ensure_offset(self, spec: SymbolSpec) -> None:
        """サーバー時刻オフセット未確定なら symbol_info_tick から1回だけ実測する。

        明示指定があれば何もしない。検出不能 (古いモック・APIなし・tick欠落) は
        0 のままにフォールバックし、例外で稼働を止めない。ブローカー非依存・DST対応。
        """
        if not self._auto_offset or self._offset_detected:
            return
        self._offset_detected = True
        mt5 = self._mt5
        if mt5 is None:
            return
        try:
            tick = mt5.symbol_info_tick(spec.name)
            t = getattr(tick, "time", 0) if tick is not None else 0
            if t:
                now = utc_now().timestamp()
                self._offset = timedelta(hours=round((t - now) / 3600.0))
        except Exception:                       # noqa: BLE001 — 検出失敗は 0 へフォールバック
            pass

    def _row_to_candle(self, row: Any, spec: SymbolSpec, tf: Timeframe) -> Candle:
        """MT5 の rates 1行 (numpy structured row, float価格) → Candle。

        ★ float→整数ティック変換はこの境界で即時に行う (CLAUDE.md 第6条)。
        """
        return Candle(
            symbol=spec.name,
            tf=tf,
            open_time=self._server_time_to_utc(row["time"]),
            o_int=spec.float_to_ticks(float(row["open"])),
            h_int=spec.float_to_ticks(float(row["high"])),
            l_int=spec.float_to_ticks(float(row["low"])),
            c_int=spec.float_to_ticks(float(row["close"])),
            volume=int(row["tick_volume"]),
            is_closed=True,  # 呼び出し側で確定判定済みの行のみ渡すこと
        )

    @staticmethod
    def _is_closed(open_time_utc: datetime, tf: Timeframe, now_utc: datetime) -> bool:
        """確定足判定: open_time + duration <= now (CLAUDE.md 第2条)。

        D1/W1 のブローカー境界ズレは duration ベースの保守判定で吸収する
        (確定済みのものを未確定扱いすることはあっても逆は起こさない)。
        """
        return open_time_utc + tf.duration <= now_utc

    # -- ヒストリカル ---------------------------------------------------------

    def get_history(
        self,
        spec: SymbolSpec,
        tf: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        ensure_utc(start, "start")
        ensure_utc(end, "end")
        mt5 = self._require_mt5()
        self._ensure_offset(spec)

        # copy_rates_range はサーバー時刻基準で範囲指定するため逆変換して渡す
        rates = mt5.copy_rates_range(
            spec.name,
            self._tf_const(tf),
            start + self._offset,
            end + self._offset,
        )
        if rates is None:
            raise FeedError(f"copy_rates_range({spec.name},{tf}) failed: {mt5.last_error()}")

        now = utc_now()
        candles: list[Candle] = []
        for row in rates:
            open_time = self._server_time_to_utc(row["time"])
            if open_time < start or open_time >= end:
                continue
            if not self._is_closed(open_time, tf, now):
                continue  # 形成中バーは返さない
            candles.append(self._row_to_candle(row, spec, tf))
        return candles

    # -- ライブ ---------------------------------------------------------------

    def iter_closed(
        self,
        spec: SymbolSpec,
        tf: Timeframe,
        *,
        stop: threading.Event | None = None,
    ) -> Iterator[Candle]:
        """ポーリングによる確定足ストリーム (フェーズ8: 再接続+欠損バックフィル)。

        堅牢化 (MarketFeed 契約 §9):
          - 切断 (copy_rates_from_pos が None) は指数バックオフで再接続を試みる。
            上限超過で FeedError を送出し上位 watchdog に委ねる。
          - ポーリング間隔より長く処理が遅れて窓 (直近 poll_lookback 本) では
            連続性を保てない場合、`last_open` と窓の最古確定足の間を
            `get_history` で **バックフィル** し、ブローカーに実在する確定足を
            1本も取りこぼさず時系列順に流す (穴埋め=合成ではなく実バーの補完)。
          - stop セットで速やかに終了する。
        """
        last_open: datetime | None = None
        self._ensure_offset(spec)

        while stop is None or not stop.is_set():
            rates = self._poll_with_reconnect(spec, tf, stop)
            if rates is None:
                return                          # stop で中断
            now = utc_now()
            window = self._closed_candles(rates, tf, now, spec)
            for candle in self._with_backfill(window, last_open, spec, tf, now):
                if last_open is not None and candle.open_time <= last_open:
                    continue                    # 二重送出防止 (冪等)
                last_open = candle.open_time
                yield candle
            if self._interruptible_sleep(self._poll_interval_s, stop):
                return

    # -- ライブ補助 (再接続・バックフィル) ------------------------------------

    def _poll_with_reconnect(self, spec: SymbolSpec, tf: Timeframe,
                             stop: threading.Event | None):
        """rates を1回取得する。切断時は指数バックオフで再接続。

        回復したら rates を返す。stop で中断したら None。
        試行上限を超えたら FeedError (回復不能として上位へ)。
        """
        backoff = self._reconnect_base_s
        attempts = 0
        while stop is None or not stop.is_set():
            mt5 = self._require_mt5()
            rates = mt5.copy_rates_from_pos(spec.name, self._tf_const(tf), 0,
                                            self._poll_lookback)
            if rates is not None:
                return rates
            # None = 接続喪失とみなし再接続
            attempts += 1
            if (self._max_reconnect_attempts
                    and attempts > self._max_reconnect_attempts):
                raise FeedError(
                    f"feed unrecoverable after {attempts - 1} reconnect attempts: "
                    f"{mt5.last_error()}")
            if self._interruptible_sleep(backoff, stop):
                return None
            try:
                self._reinitialize()
            except FeedError:
                pass                            # 次ループでさらにバックオフして再試行
            backoff = min(backoff * 2, self._reconnect_max_s)
        return None

    def _reinitialize(self) -> None:
        """切断後の再初期化 (モジュール参照は保持して shutdown→initialize)。"""
        mt5 = self._mt5
        if mt5 is None:
            self.connect()
            return
        try:
            mt5.shutdown()
        except Exception:                       # noqa: BLE001 — shutdown失敗は無視して再init
            pass
        if not mt5.initialize(**self._init_kwargs):
            raise FeedError(f"mt5.initialize failed on reconnect: {mt5.last_error()}")
        self.reconnect_count += 1               # 復帰成功 → 上位のリコンサイル起動フック

    def _closed_candles(self, rates, tf: Timeframe, now: datetime,
                        spec: SymbolSpec) -> list[Candle]:
        """rates 窓のうち確定済みの行を Candle 化し open_time 昇順で返す。"""
        out: list[Candle] = []
        for row in rates:
            open_time = self._server_time_to_utc(row["time"])
            if not self._is_closed(open_time, tf, now):
                continue                        # 形成中バーは流さない (CLAUDE.md 第2条)
            out.append(self._row_to_candle(row, spec, tf))
        out.sort(key=lambda c: c.open_time)
        return out

    def _with_backfill(self, window: list[Candle], last_open: datetime | None,
                       spec: SymbolSpec, tf: Timeframe,
                       now: datetime) -> list[Candle]:
        """窓と last_open の間に欠損があれば実バーを get_history で補完して返す。"""
        if last_open is None or not window:
            return window
        new = [c for c in window if c.open_time > last_open]
        if not new:
            return window
        gap_start = last_open + tf.duration
        first_new = new[0].open_time
        if first_new <= gap_start:
            return window                       # 連続している (欠損なし)
        # 窓が last_open の直後に届いていない = ポーリング落伍/切断中の取りこぼし。
        # [gap_start, first_new) の実在確定足を取得して前置する。
        missing = self.get_history(spec, tf, gap_start, first_new)
        merged: dict[datetime, Candle] = {c.open_time: c for c in missing}
        for c in window:
            merged[c.open_time] = c
        return [merged[k] for k in sorted(merged)]

    def _interruptible_sleep(self, seconds: float,
                             stop: threading.Event | None) -> bool:
        """stop を監視しつつ seconds 待つ。停止したら True。"""
        if stop is None:
            time.sleep(seconds)
            return False
        return stop.wait(seconds)               # set 済みなら True を即返す

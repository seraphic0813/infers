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
from infers.data.models import Candle, SymbolSpec, Timeframe, utc_now


class MT5Feed(MarketFeed):
    """MetaTrader5 ターミナル経由のフィード実装(骨組み)。

    Vantage Trading / Swift Trader はいずれも MT5 ターミナルを介して接続する。
    ターミナルの起動・ログイン情報は config/broker.yaml から渡される想定。
    """

    def __init__(
        self,
        *,
        server_utc_offset: timedelta = timedelta(0),
        poll_interval_s: float = 1.0,
        terminal_path: str | None = None,
        login: int | None = None,
        password: str | None = None,   # 環境変数から注入。ログ出力禁止 (CLAUDE.md 開発環境)
        server: str | None = None,
    ) -> None:
        # server_utc_offset: ブローカーサーバー時刻 − UTC。broker.yaml で銘柄/口座別に定義。
        # MT5 の rates['time'] はサーバー時刻基準の epoch 秒であり、ここで UTC へ正規化する。
        self._offset = server_utc_offset
        self._poll_interval_s = poll_interval_s
        self._init_kwargs: dict[str, Any] = {}
        if terminal_path:
            self._init_kwargs["path"] = terminal_path
        if login is not None:
            self._init_kwargs.update(login=login, password=password, server=server)
        self._mt5: Any | None = None  # 遅延 import した MetaTrader5 モジュール

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
        """ポーリングによる確定足ストリーム(骨組み)。

        TODO(フェーズ8): 再接続(指数バックオフ)・欠損検出時のバックフィル・
        リコンサイル連携。現状は単純ポーリングの参照実装。
        """
        mt5 = self._require_mt5()
        last_open: datetime | None = None

        while stop is None or not stop.is_set():
            # 直近3本を取得し、未送出かつ確定済みのバーだけを時系列順に流す
            rates = mt5.copy_rates_from_pos(spec.name, self._tf_const(tf), 0, 3)
            if rates is None:
                raise FeedError(f"copy_rates_from_pos failed: {mt5.last_error()}")
            now = utc_now()
            for row in rates:
                open_time = self._server_time_to_utc(row["time"])
                if last_open is not None and open_time <= last_open:
                    continue
                if not self._is_closed(open_time, tf, now):
                    continue
                candle = self._row_to_candle(row, spec, tf)
                last_open = candle.open_time
                yield candle
            time.sleep(self._poll_interval_s)

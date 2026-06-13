"""マーケットフィード抽象インターフェース (設計書 §1.1 / §2)。

戦略コアは本インターフェースのみに依存する (CLAUDE.md 第12条)。
実装はライブ (mt5_feed.MT5Feed) とバックテスト (HistoricalFeed, フェーズ4) の
2系統で、どちらも「確定足のみを時系列順に供給する」契約を満たすこと。
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Iterator

from infers.data.models import Candle, SymbolSpec, Timeframe


class FeedError(RuntimeError):
    """フィード層の回復不能エラー(接続不能・データ異常)。"""


class MarketFeed(ABC):
    """確定足供給の抽象基底。

    契約 (全実装が満たすべき不変条件):
      1. 返す Candle はすべて is_closed=True (確定足主義: CLAUDE.md 第2条)
      2. open_time は tz-aware UTC へ正規化済み (第7条。サーバー時刻
         オフセットの吸収はアダプタの責務)
      3. 同一 (symbol, tf) 内で open_time は厳密単調増加・欠損は呼び出し側へ
         そのまま見せる(穴埋めしない。バックフィルは別途明示的に行う)
    """

    # -- ライフサイクル -----------------------------------------------------

    @abstractmethod
    def connect(self) -> None:
        """接続を確立する。失敗時は FeedError。冪等であること。"""

    @abstractmethod
    def close(self) -> None:
        """接続を解放する。未接続でも安全に呼べること。"""

    def __enter__(self) -> "MarketFeed":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- ヒストリカル ---------------------------------------------------------

    @abstractmethod
    def get_history(
        self,
        spec: SymbolSpec,
        tf: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        """[start, end) の確定足を時系列昇順で返す。

        - start / end は tz-aware UTC 必須(naive は ValueError)
        - end 時点で未確定のバーは含めない
        - 対応 tf: M5, M15, H1, H4, D1, W1
        """

    # -- ライブ ---------------------------------------------------------------

    @abstractmethod
    def iter_closed(
        self,
        spec: SymbolSpec,
        tf: Timeframe,
        *,
        stop: threading.Event | None = None,
    ) -> Iterator[Candle]:
        """新しい確定足が生まれるたびに 1 本ずつ yield する(ブロッキング)。

        - 形成中のバーは決して yield しない (CLAUDE.md 第2条)
        - 接続断からの再接続・リコンサイルは実装側の責務
          (指数バックオフ。回復不能なら FeedError を送出して上位の
           watchdog に委ねる — 設計書 §9)
        - stop イベントがセットされたら速やかに StopIteration で終了する
        """


def ensure_utc(dt: datetime, name: str) -> datetime:
    """引数検証ヘルパー: tz-aware UTC でなければ ValueError (CLAUDE.md 第7条)。"""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{name} must be tz-aware UTC, got naive datetime: {dt!r}")
    return dt

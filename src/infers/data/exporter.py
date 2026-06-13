"""ヒストリカルデータ統合スクリプト (設計書 §2 / フェーズ8)。

MT5 (またはあらゆる MarketFeed 実装) から過去データをチャンク分割で吸い上げ、
境界で即座に整数ティック化された Candle として Parquet に保存する。

保存形式 (INFERSネイティブ・整数ティック形式):
  列: symbol(str), tf(str), time(datetime UTC),
      o_int/h_int/l_int/c_int(int64), volume(int64)
価格は整数ティックのまま保存するため、読み込み時に float を経由しない
(CLAUDE.md 第6条 — float が境界を越えない)。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Sequence

from infers.data.feed import MarketFeed, ensure_utc
from infers.data.models import Candle, SymbolSpec, Timeframe

# 1回の get_history で取得する期間 (MT5のバー数上限対策)
DEFAULT_CHUNK = timedelta(days=30)

Writer = Callable[[Sequence[dict], Path], None]


def candles_to_rows(candles: Sequence[Candle]) -> list[dict]:
    return [{
        "symbol": c.symbol, "tf": c.tf.value, "time": c.open_time,
        "o_int": c.o_int, "h_int": c.h_int, "l_int": c.l_int, "c_int": c.c_int,
        "volume": c.volume,
    } for c in candles]


def _write_parquet(rows: Sequence[dict], path: Path) -> None:
    try:
        import polars as pl  # 遅延 import (CIでは writer を差し替えてテスト)
    except ImportError as e:
        raise RuntimeError("polars required: pip install polars") from e
    pl.DataFrame(rows).write_parquet(str(path))


def export_history(
    feed: MarketFeed,
    spec: SymbolSpec,
    tf: Timeframe,
    start: datetime,
    end: datetime,
    out_path: str | Path,
    *,
    chunk: timedelta = DEFAULT_CHUNK,
    writer: Writer = _write_parquet,
) -> int:
    """[start, end) の確定足をチャンク取得し Parquet へ保存。件数を返す。

    - 確定足のみ (MarketFeed の契約)
    - チャンク境界の重複は open_time で排除し、時系列昇順で保存
    """
    ensure_utc(start, "start")
    ensure_utc(end, "end")
    if start >= end:
        raise ValueError("start must be before end")

    by_time: dict[datetime, Candle] = {}
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + chunk, end)
        for candle in feed.get_history(spec, tf, cursor, chunk_end):
            by_time[candle.open_time] = candle      # 重複は後勝ち (同一データ)
        cursor = chunk_end

    ordered = [by_time[t] for t in sorted(by_time)]
    writer(candles_to_rows(ordered), Path(out_path))
    return len(ordered)


def load_history(path: str | Path, *, tf: Timeframe | None = None) -> list[Candle]:
    """INFERSネイティブ形式の Parquet を読み込む (float を経由しない)。"""
    try:
        import polars as pl  # 遅延 import
    except ImportError as e:
        raise RuntimeError("polars required: pip install polars") from e

    df = pl.read_parquet(str(path)).sort("time")
    candles: list[Candle] = []
    for row in df.iter_rows(named=True):
        row_tf = Timeframe(row["tf"])
        if tf is not None and row_tf is not tf:
            continue
        candles.append(Candle(
            symbol=row["symbol"], tf=row_tf, open_time=row["time"],
            o_int=int(row["o_int"]), h_int=int(row["h_int"]),
            l_int=int(row["l_int"]), c_int=int(row["c_int"]),
            volume=int(row["volume"]), is_closed=True,
        ))
    return candles

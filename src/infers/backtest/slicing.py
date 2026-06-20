"""バックテストの期間スライス (段階2.4 / docs/phase2-architecture.md §5)。

5年分の Parquet 1ファイルを正とし、フルテスト/ライトテストはすべて
読込後のローソク足列を時刻でフィルタすることで表現する(別ファイルを増やさない)。
BacktestEngine.run は Iterable[Candle] を受けるだけなので、エンジン本体は不変。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Sequence

from infers.core.models import Candle

# 相対期間トークン (例: "1y", "6m", "90d", "4w") → 日数換算。
# 暦月/暦年の厳密性より決定論性・単純さを優先する(y=365日, m=30日)。
_UNIT_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365}
_REL_RE = re.compile(r"^\s*(\d+)\s*([dwmy])\s*$", re.IGNORECASE)


def parse_relative(token: str) -> timedelta:
    """'1y' / '6m' / '90d' / '4w' を timedelta に変換する。"""
    match = _REL_RE.match(token)
    if not match:
        raise ValueError(
            f"invalid relative period {token!r} (expected like '1y', '6m', '90d', '4w')")
    n, unit = int(match.group(1)), match.group(2).lower()
    return timedelta(days=n * _UNIT_DAYS[unit])


def _to_utc(dt: datetime) -> datetime:
    """naive datetime は UTC とみなす(CLAUDE.md 第7条: 判定はUTC固定)。"""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def slice_candles(
    candles: Sequence[Candle],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    last: str | None = None,
) -> list[Candle]:
    """確定足列を期間で切り出す(両端含む)。

    - start / end: UTC の絶対範囲(naive は UTC とみなす)。
    - last: データ終端からの相対期間(例 '1y')。指定時は終端 - last を start と
      する(明示 start より優先)。end は併用可。
    候補が空、または start > end の場合は空リストを返す(呼び出し側が判断)。
    """
    if not candles:
        return []
    if last is not None:
        end_anchor = _to_utc(candles[-1].open_time) if end is None else _to_utc(end)
        start = end_anchor - parse_relative(last)
    s = _to_utc(start) if start is not None else None
    e = _to_utc(end) if end is not None else None
    out = []
    for c in candles:
        t = _to_utc(c.open_time)
        if s is not None and t < s:
            continue
        if e is not None and t > e:
            continue
        out.append(c)
    return out

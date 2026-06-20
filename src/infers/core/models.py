"""データモデル定義 (設計書 §2 / CLAUDE.md 第2・6・7・8条)。

価格は整数ティック (``*_int``) で保持する。Decimal とティックの相互変換は
:class:`SymbolSpec` のみが担い、float の価格表現がモジュール境界を越えることは
ない (float が許されるのは外部API境界での即時変換のみ)。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ティック数への変換時に許容する量子化誤差(ティック比)。
# ブローカー価格は本来 tick_size の整数倍であり、float→Decimal 変換の
# 丸め残差のみをここで吸収する。超えた場合はデータ異常として例外。
_QUANTIZE_TOLERANCE = Decimal("0.01")


class Timeframe(str, Enum):
    """対応する時間足 (設計書 §0 前提条件)。"""

    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"
    W1 = "W1"

    @property
    def duration(self) -> timedelta:
        """1バーの長さ。確定足判定 (open_time + duration <= now) に用いる。

        D1/W1 はブローカーの日次/週次境界に依存するため、厳密な境界規則は
        フィードアダプタが broker.yaml のオフセットで補正する (CLAUDE.md 第7条)。
        """
        return _DURATIONS[self]


_DURATIONS: dict[Timeframe, timedelta] = {
    Timeframe.M5: timedelta(minutes=5),
    Timeframe.M15: timedelta(minutes=15),
    Timeframe.M30: timedelta(minutes=30),
    Timeframe.H1: timedelta(hours=1),
    Timeframe.H4: timedelta(hours=4),
    Timeframe.D1: timedelta(days=1),
    Timeframe.W1: timedelta(weeks=1),
}


class SymbolSpec(BaseModel):
    """銘柄仕様。価格⇄整数ティック変換の唯一の入口 (CLAUDE.md 第6条)。"""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)            # 例 "XAUUSD"
    tick_size: Decimal = Field(gt=0)           # 例 Decimal("0.01")
    lot_step: Decimal = Field(gt=0)            # 例 Decimal("0.01")
    digits: int = Field(ge=0)                  # 表示桁数 (from_ticks の quantize 用)

    def to_ticks(self, price: Decimal | str | int) -> int:
        """Decimal 価格を整数ティックへ変換する。

        tick_size の整数倍から _QUANTIZE_TOLERANCE を超えて外れる価格は
        データ異常とみなし ValueError を送出する。
        """
        ratio = Decimal(price) / self.tick_size
        ticks = int(ratio.to_integral_value(rounding=ROUND_HALF_EVEN))
        if abs(ratio - ticks) > _QUANTIZE_TOLERANCE:
            raise ValueError(
                f"{self.name}: price {price} is not a multiple of tick_size {self.tick_size}"
            )
        return ticks

    def float_to_ticks(self, price: float) -> int:
        """外部API境界(MT5等の float 価格)専用の即時変換。

        repr() 経由で float の最短十進表現を取り、以降は Decimal/int のみで扱う。
        この関数の呼び出し元以外で float 価格を保持してはならない。
        """
        return self.to_ticks(Decimal(repr(price)))

    def from_ticks(self, ticks: int) -> Decimal:
        """整数ティックを Decimal 価格へ戻す(表示・API送信境界用)。"""
        return (ticks * self.tick_size).quantize(Decimal(1).scaleb(-self.digits))


class Candle(BaseModel):
    """確定足イベント (設計書 §2)。

    - 価格4値は整数ティック (CLAUDE.md 第6条)
    - open_time は tz-aware UTC のみ (第7条)
    - 下流の分析層には is_closed=True のみを流す (第2条・確定足主義)
    """

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1)
    tf: Timeframe
    open_time: datetime
    o_int: int
    h_int: int
    l_int: int
    c_int: int
    volume: int = Field(ge=0)
    is_closed: bool

    @model_validator(mode="after")
    def _validate(self) -> "Candle":
        if self.open_time.tzinfo is None or self.open_time.utcoffset() != timedelta(0):
            raise ValueError("open_time must be tz-aware UTC (CLAUDE.md rule 7)")
        if self.h_int < self.l_int:
            raise ValueError(f"high {self.h_int} < low {self.l_int}")
        if not (self.l_int <= self.o_int <= self.h_int):
            raise ValueError("open outside [low, high]")
        if not (self.l_int <= self.c_int <= self.h_int):
            raise ValueError("close outside [low, high]")
        return self

    @property
    def close_time(self) -> datetime:
        """このバーが確定する時刻 (= 次バーの open_time の基準)。"""
        return self.open_time + self.tf.duration

    @classmethod
    def from_decimal(
        cls,
        spec: SymbolSpec,
        *,
        tf: Timeframe,
        open_time: datetime,
        o: Decimal | str,
        h: Decimal | str,
        l: Decimal | str,
        c: Decimal | str,
        volume: int,
        is_closed: bool = True,
    ) -> "Candle":
        """Decimal 価格からの生成ヘルパー(変換は SymbolSpec に集約)。"""
        return cls(
            symbol=spec.name,
            tf=tf,
            open_time=open_time,
            o_int=spec.to_ticks(o),
            h_int=spec.to_ticks(h),
            l_int=spec.to_ticks(l),
            c_int=spec.to_ticks(c),
            volume=volume,
            is_closed=is_closed,
        )


def utc_now() -> datetime:
    """tz-aware UTC の現在時刻。確定足判定の比較にのみ用いる。

    売買判断そのものはフィードのサーバー時刻基準で行う (CLAUDE.md 第7条)。
    """
    return datetime.now(timezone.utc)

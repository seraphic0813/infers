"""Narrow Focus 手法のシグナル/プラン語彙 (L2 / 段階2.3b)。

`TradePlan`(打診プラン)と `ProviderOutput`(毎確定足の分析出力)は、追撃トリガー
`w1_high_int`・フィボ目標 `fib_target_int`・ダウ構造イベント `structure_events`・
半利トリガー(RSI/90SMA/重要SRゾーン)など Narrow Focus 固有の語彙を持つため、
L0(`core/`)ではなく手法側(L2)に属する。`StructureEvent`/`SRZone` を import するのも
同手法内(L2→L2)なので層境界に反しない。

`TradingLoop`(L0)はこれらを型としては知らず、`ExecutionModel.on_bar(candle, signal)`
の `signal`(object)/`open_positions` のプラン要素(object)として不透明に扱う。
後方互換のため `infers.core.loop` からも遅延 re-export される(同モジュールの __getattr__)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from infers.ai.gateway import JudgementRequest
from infers.analysis.dow import StructureEvent
from infers.analysis.support_resistance import SRZone


@dataclass(frozen=True)
class TradePlan:
    """組み上げた打診プラン (AIゲート審査前)。価格はすべて整数ティック。"""

    plan_id: str
    direction: int
    limit_price_int: int
    volume_steps: int
    add_volume_steps: int
    sl_int: int
    expiry: datetime
    invalidation_price: int
    w1_high_int: int                  # 追撃トリガーの基準 (設計書 §6.2)
    fib_target_int: int               # 半分利確の161.8%目標 (設計書 §6.4)
    request: JudgementRequest
    cluster_score: Decimal
    ambiguity: Decimal
    # エントリー根拠の可視化用 (省略可。ライブ執行には不要)。
    w1_low_int: int = 0                          # 第1波起点 (FIB押し戻しの基準)
    fib_levels: tuple[int, ...] = ()             # 第1波の押し戻し水準価格 (38.2/50/61.8/78.6)
    sr_zones: tuple[tuple[int, int], ...] = ()   # エントリー近傍の重要SRゾーン (low,high)


@dataclass
class ProviderOutput:
    plans: list[TradePlan] = field(default_factory=list)
    structure_events: list[StructureEvent] = field(default_factory=list)
    rsi_value: Decimal | None = None              # 現在RSI (半分利確トリガー: §6.4)
    tp_sr_zones: tuple[SRZone, ...] = ()          # 重要SRゾーン (半分利確トリガー: §6.4)
    sma90_int: int | None = None                  # 90SMA値 (半分利確トリガー: ③-1)

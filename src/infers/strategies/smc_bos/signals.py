"""smc_bos 手法のシグナル語彙 (L2 / 段階S4 / spec.md §5.1)。

`SmcOutput` は毎確定足の分析出力。`plans` は `narrow_focus.signals.TradePlan` を
共通語彙として流用する(market_tpsl と同じ方針)。`swing_high_int`/
`swing_low_int` は `SmcExecution.on_bar` への管理シグナル(`be_mode=structure`
のSL前進トリガー。spec.md §3.3・§3.4)。

`TradingLoop`(L0)は `output.plans` のみを duck-typing で読み、`output` 自体は
`ExecutionModel.on_bar(candle, signal)` への不透明な引き渡しに使うだけなので、
`narrow_focus.signals.ProviderOutput`(`structure_events`/`tp_sr_zones`/
`sma90_int` 等、Narrow Focus 固有の語彙を持つ)を再利用する必要はない。
本手法専用の最小フィールドに絞ることで、L2→L2 の不要な型結合を増やさない。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from infers.strategies.narrow_focus.signals import TradePlan


@dataclass
class SmcOutput:
    plans: list[TradePlan] = field(default_factory=list)
    swing_high_int: int | None = None   # ショート保護用 (直近確定スイング高値)
    swing_low_int: int | None = None    # ロング保護用 (直近確定スイング安値)

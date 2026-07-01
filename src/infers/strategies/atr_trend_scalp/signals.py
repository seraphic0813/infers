"""atr_trend_scalp 手法のシグナル/プラン語彙 (L2 / spec.md §5.1)。

`AtrTrendPlan` は本手法の参入プラン。分割決済(TP1で半利+建値化、残玉トレール
+ 最終TP)のため、smc_bos/market_tpsl のように `narrow_focus.signals.TradePlan`
を流用せず専用化する。TP1価格・トレール幅という追加ジオメトリを明示フィールドで
持たせるためで、TradePlan の未使用フィールドへ詰めるより可読性が高い。

`TradingLoop`(L0)は `output.plans` のみを duck-typing で読む。プランは
`dataclasses.replace(plan, volume_steps=..., add_volume_steps=...)` で可変ロット時に
再構築されるため **frozen dataclass** である必要がある。`backtest/report_html.py`
の `_plan_dict` が TradePlan 相当のフィールド(w1_low_int/fib_levels/sr_zones 等)を
参照するため、それらを中立値で保持して互換を取る(report_html は手法非依存のまま)。

`AtrTrendOutput` は毎確定足の分析出力。`plans` に加え、可視化/レポート用の最新
指標値を保持する。L0 は `output.plans` のみ読み、`output` 自体は
`ExecutionModel.on_bar(candle, signal)` へ不透明に引き渡される。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from infers.ai.gateway import JudgementRequest


@dataclass(frozen=True)
class AtrTrendPlan:
    """組み上げた参入プラン(AIゲート審査前)。価格はすべて整数ティック。

    分割決済ジオメトリ(`tp1_int`/`trail_distance_ticks`)は provider が
    エントリー時ATRから確定させ、`AtrTrendExecution` がそのまま読む。半玉数は
    可変ロット(`--risk-pct`)で volume_steps が上書きされ得るため **プランには
    焼き込まず**、執行側が place() 時点の実 volume_steps から導出する。
    """

    plan_id: str
    direction: int
    limit_price_int: int              # 参入参考価格(=確定足終値)
    volume_steps: int                 # 総建玉(50/50分割のため偶数を推奨)
    sl_int: int                       # 初期SL(= entry ∓ 1.0×ATR、min距離ガード込み)
    expiry: datetime                  # 成行のため遠未来(loop互換のプレースホルダ)
    invalidation_price: int           # = 初期SL
    fib_target_int: int               # 第2利確 TP2(= entry ± 2.0×ATR)
    tp1_int: int                      # 第1利確 TP1(= entry ± 1.0×ATR)
    trail_distance_ticks: int         # 残玉トレール幅(= round(0.5×ATR)、エントリー時ATR固定)
    atr_at_entry_int: int             # エントリー時ATR(ティック。可視化・根拠)
    request: JudgementRequest
    cluster_score: Decimal
    ambiguity: Decimal
    # -- 以下は loop / report_html 互換のための中立フィールド(本手法では未使用) --
    add_volume_steps: int = 0
    w1_high_int: int = 0
    w1_low_int: int = 0
    fib_levels: tuple[int, ...] = ()
    sr_zones: tuple[tuple[int, int], ...] = ()


@dataclass
class AtrTrendOutput:
    """毎確定足の分析出力。`plans` は 0 または 1 件。"""

    plans: list[AtrTrendPlan] = field(default_factory=list)
    # 可視化/レポート用の最新指標値(ウォームアップ中は None)。
    ema_fast: Decimal | None = None       # EMA9 (M5)
    ema_medium: Decimal | None = None     # EMA21 (M5)
    ema_trend: Decimal | None = None      # EMA50 (M15・上位足)
    atr: Decimal | None = None            # ATR14 (M5)
    vol_sma: Decimal | None = None        # 出来高SMA20 (M5)

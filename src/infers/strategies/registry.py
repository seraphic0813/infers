"""手法レジストリ (段階2.2 / docs/phase2-architecture.md §4.4)。

名前で手法(SignalProviderの組み立て方)を引けるようにする。各エントリは
「確認済みの構成」を1つの名前に束ねた便宜的なプリセットであり、
`infers.main` の個別CLIフラグ(`--depth-max` 等)による既存の組み立て経路は
本レジストリ導入後も完全に維持される(`--strategy` 未指定時は1ビットも
挙動が変わらない)。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from infers.core.execution import ExecutionModel
from infers.core.loop import SignalProvider
from infers.core.models import Timeframe
from infers.core.report_spec import StrategyReportSpec
from infers.strategies.narrow_focus.report import NARROW_FOCUS_REPORT_SPEC
from infers.strategies.smc_bos.report import SMC_BOS_REPORT_SPEC


@dataclass(frozen=True)
class StrategySpec:
    name: str
    build: Callable[..., SignalProvider]
    description: str
    # 執行モデルの生成器 (段階2.5)。手法は分析(Strategy/SignalProvider)と
    # 執行ライフサイクル(ExecutionModel)の組で1つを成す。None のとき TradingLoop は
    # 既定の Narrow Focus 執行 (NarrowFocusExecution) を使う(narrow_focus/depth50 は
    # これに該当し、執行経路は従来と1ビットも変わらない)。シグネチャは
    # (position_id, direction, broker, config, journal_sink) -> ExecutionModel。
    build_execution: "Callable[..., ExecutionModel] | None" = None
    # レポート表示記述子 (report_html.py)。None のとき main.py は手法非依存の
    # 汎用フォールバック(GENERIC_REPORT_SPEC)を使う。
    report_spec: "StrategyReportSpec | None" = None


_REGISTRY: dict[str, StrategySpec] = {}


def register(spec: StrategySpec) -> None:
    if spec.name in _REGISTRY:
        raise ValueError(f"strategy {spec.name!r} is already registered")
    _REGISTRY[spec.name] = spec


def get_strategy(name: str) -> StrategySpec:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"unknown strategy {name!r}. registered: {sorted(_REGISTRY)}") from None


def strategy_names() -> list[str]:
    return sorted(_REGISTRY)


def _build_narrow_focus(*, symbol: str, tf: Timeframe) -> SignalProvider:
    """ProviderConfig の素の既定値 (フラグ無指定の従来挙動と同一)。"""
    from infers.strategies.narrow_focus.provider import InfersSignalProvider, ProviderConfig
    return InfersSignalProvider(symbol=symbol, tf=tf, config=ProviderConfig())


def _build_depth50(*, symbol: str, tf: Timeframe) -> SignalProvider:
    """v1.0確定ベースライン (reports/README.md再生成コマンドと同一構成)。

    --macro-wave2 --depth-screen --depth-max 0.50 --no-fib-score 相当。
    """
    from infers.strategies.narrow_focus.provider import InfersSignalProvider, ProviderConfig
    cfg = ProviderConfig(macro_wave2=True, depth_screen=True,
                         depth_max=Decimal("0.50"), score_fib=False)
    return InfersSignalProvider(symbol=symbol, tf=tf, config=cfg)


register(StrategySpec(
    name="narrow_focus",
    build=_build_narrow_focus,
    description="Narrow Focus 分析パイプライン (ProviderConfig既定値。フラグ未指定時の従来挙動と同一)",
    report_spec=NARROW_FOCUS_REPORT_SPEC,
))
register(StrategySpec(
    name="depth50",
    build=_build_depth50,
    description="v1.0確定ベースライン (riskfix+40%深さスクリーニングを50%へ緩和)",
    report_spec=NARROW_FOCUS_REPORT_SPEC,
))


def _build_market_tpsl(*, symbol: str, tf: Timeframe) -> SignalProvider:
    """SMAクロス手法の分析層 (段階2.5: 執行モデル抽象の実証用)。"""
    from infers.strategies.market_tpsl.provider import SmaCrossProvider
    return SmaCrossProvider(symbol=symbol, tf=tf)


def _build_market_execution(*, position_id, direction, broker, config,
                            journal_sink=None) -> ExecutionModel:
    """成行参入+固定TP/SL の執行モデル生成器 (TradingLoop へ注入)。"""
    from infers.strategies.market_tpsl.execution import MarketTpSlExecution
    return MarketTpSlExecution(position_id=position_id, direction=direction,
                               broker=broker, config=config, journal_sink=journal_sink)


register(StrategySpec(
    name="market_tpsl",
    build=_build_market_tpsl,
    build_execution=_build_market_execution,
    description="SMAクロス + 成行参入/固定TP/SL (Narrow Focus とは別の執行ライフサイクル。"
                "TradingLoop が執行モデル非依存であることの実証用)",
))


def _build_smc_bos(*, symbol: str, tf: Timeframe) -> SignalProvider:
    """M30 SMC BOS + EMA80 手法の分析層 (段階S2。spec.md §5.1)。

    判定TFはM30想定だが、レジストリの呼び出し規約に合わせ呼び出し元が渡す
    tf をそのまま使う(CLI利用時は `--tf M30` を明示すること。spec.md §5.5)。
    """
    from infers.strategies.smc_bos.provider import SmcBosProvider
    return SmcBosProvider(symbol=symbol, tf=tf)


def _build_smc_execution(*, position_id, direction, broker, config,
                         journal_sink=None) -> ExecutionModel:
    """成行参入+固定SL/RR利確 の執行モデル生成器 (段階S2。spec.md §3.4)。"""
    from infers.strategies.smc_bos.execution import SmcExecution
    return SmcExecution(position_id=position_id, direction=direction,
                        broker=broker, config=config, journal_sink=journal_sink)


register(StrategySpec(
    name="smc_bos",
    build=_build_smc_bos,
    build_execution=_build_smc_execution,
    description="M30 SMC BOS(Break of Structure) + EMA80フィルタ (XAUUSD)。"
                "構造ブレイク成行参入 + 固定SL/RR利確 (段階S2: SL前進はS4で追加)。"
                "Narrow Focus / market_tpsl とは別の執行ライフサイクル",
    report_spec=SMC_BOS_REPORT_SPEC,
))

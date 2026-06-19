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

from infers.core.loop import SignalProvider
from infers.core.models import Timeframe


@dataclass(frozen=True)
class StrategySpec:
    name: str
    build: Callable[..., SignalProvider]
    description: str


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
))
register(StrategySpec(
    name="depth50",
    build=_build_depth50,
    description="v1.0確定ベースライン (riskfix+40%深さスクリーニングを50%へ緩和)",
))

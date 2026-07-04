"""narrow_focus_mtf レジストリ配線テスト(改善プラン T16 M1)。

`src/infers/strategies/narrow_focus/mtf-variant.md` §2.2 の設計どおりに
レジストリ登録されていることを検証する(provider/execution本体の挙動は
既存の narrow_focus/depth50 テストでカバー済みのため、ここでは配線のみ)。
"""

from __future__ import annotations

from decimal import Decimal

from infers.core.models import Timeframe
from infers.strategies.narrow_focus.provider import InfersSignalProvider, ProviderConfig
from infers.strategies.narrow_focus.report import NARROW_FOCUS_REPORT_SPEC
from infers.strategies.registry import get_strategy


class TestNarrowFocusMtfRegistration:
    def test_registered_with_rule_gate(self):
        assert get_strategy("narrow_focus_mtf").gate == "rule"

    def test_uses_narrow_focus_report_spec(self):
        assert get_strategy("narrow_focus_mtf").report_spec is NARROW_FOCUS_REPORT_SPEC

    def test_build_execution_is_none_uses_default_narrow_focus_execution(self):
        assert get_strategy("narrow_focus_mtf").build_execution is None

    def test_build_returns_infers_signal_provider(self):
        provider = get_strategy("narrow_focus_mtf").build(symbol="XAUUSD", tf=Timeframe.H1)
        assert isinstance(provider, InfersSignalProvider)
        assert provider.symbol == "XAUUSD"
        assert provider.tf == Timeframe.H1

    def test_config_matches_depth50_filters_with_h1_rsi_macro_tfs(self):
        """depth50と同一フィルター構成 (macro_wave2/depth_screen/depth_max=0.50/
        score_fib=False) を引き継ぎ、rsi_macro_tfs のみ (H4, D1) へ変更されていること。
        macro_tf(D1)・wave2_tf(H4)は既定のまま(単一変数実験)。"""
        provider = get_strategy("narrow_focus_mtf").build(symbol="XAUUSD", tf=Timeframe.H1)
        cfg = provider.cfg
        default_cfg = ProviderConfig()
        assert cfg.macro_wave2 is True
        assert cfg.depth_screen is True
        assert cfg.depth_max == Decimal("0.50")
        assert cfg.score_fib is False
        assert cfg.rsi_macro_tfs == (Timeframe.H4, Timeframe.D1)
        assert cfg.macro_tf == default_cfg.macro_tf
        assert cfg.wave2_tf == default_cfg.wave2_tf

    def test_registered_in_strategy_names(self):
        from infers.strategies.registry import strategy_names
        assert "narrow_focus_mtf" in strategy_names()

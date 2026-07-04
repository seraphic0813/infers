"""tools/sweep.py のテスト(改善プランT9)。

実データファイル(data/*.parquet)には依存しない — 既に登録済みの `market_tpsl`
手法(`SmaCrossProvider` + `MarketTpSlExecution`)を合成ローソク足で駆動し、
ハーネスの配線(provider構築・ベースライン自己検証・スイープ・振動検出)を
end-to-endで検証する。
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from infers.core.models import Candle, Timeframe, SymbolSpec  # noqa: E402
from infers.strategies.market_tpsl.provider import SmaCrossProvider  # noqa: E402
from infers.strategies.registry import get_strategy  # noqa: E402

from sweep import (  # noqa: E402
    SweepBaselineMismatch, SweepConfig, _coerce_value, _parse_param_arg,
    build_provider_instance, detect_oscillation, run_one, self_verify_baseline, sweep,
)

UTC = timezone.utc
T0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
GOLD = SymbolSpec(name="XAUUSD", tick_size=Decimal("0.01"), lot_step=Decimal("0.01"), digits=2)
EXECUTION_FACTORY = get_strategy("market_tpsl").build_execution


def _zigzag_candles(n_cycles: int = 20, amplitude: int = 30) -> list[Candle]:
    """SMA(2)/SMA(3)クロスを繰り返し発生させる合成ジグザグ列。"""
    candles: list[Candle] = []
    price = 1000
    i = 0
    for _ in range(n_cycles):
        for step in (amplitude, amplitude, -amplitude, -amplitude):
            price += step
            h, l = max(price, price - step), min(price, price - step)
            candles.append(Candle(
                symbol="XAUUSD", tf=Timeframe.M5,
                open_time=T0 + i * Timeframe.M5.duration,
                o_int=price - step, h_int=h + 5, l_int=l - 5, c_int=price,
                volume=1, is_closed=True))
            i += 1
    return candles


CANDLES = _zigzag_candles()


# ---------------------------------------------------------------------------
# 単体: 値変換・パラメータ解析・振動検出
# ---------------------------------------------------------------------------

class TestCoerceValue:
    def test_decimal_default_coerces_to_decimal(self):
        assert _coerce_value(Decimal("1.5"), "2.0") == Decimal("2.0")

    def test_bool_default_coerces_truthy_strings(self):
        assert _coerce_value(False, "true") is True
        assert _coerce_value(False, "0") is False

    def test_int_default_coerces_to_int(self):
        assert _coerce_value(20, "30") == 30

    def test_timeframe_default_coerces(self):
        assert _coerce_value(Timeframe.M5, "M15") is Timeframe.M15

    def test_str_default_passthrough(self):
        assert _coerce_value("foo", "bar") == "bar"


class TestParseParamArg:
    def test_valid(self):
        assert _parse_param_arg("vol_mult=1.2,1.45,1.6") == ("vol_mult", ["1.2", "1.45", "1.6"])

    def test_missing_equals_raises(self):
        with pytest.raises(ValueError):
            _parse_param_arg("vol_mult")

    def test_missing_values_raises(self):
        with pytest.raises(ValueError):
            _parse_param_arg("vol_mult=")


class TestDetectOscillation:
    def test_monotonic_increasing_is_not_oscillating(self):
        assert not detect_oscillation([Decimal(1), Decimal(2), Decimal(3), Decimal(4)])

    def test_single_peak_is_not_oscillating(self):
        assert not detect_oscillation([Decimal(1), Decimal(3), Decimal(2)])

    def test_zigzag_two_reversals_is_oscillating(self):
        assert detect_oscillation([Decimal(1), Decimal(3), Decimal(1), Decimal(3)])

    def test_too_few_points_is_not_oscillating(self):
        assert not detect_oscillation([Decimal(1), Decimal(2)])

    def test_none_values_are_ignored(self):
        assert not detect_oscillation([Decimal(1), None, Decimal(2), Decimal(3)])


class TestBuildProviderInstance:
    def test_overrides_applied_with_type_coercion(self):
        provider = build_provider_instance(
            SmaCrossProvider, symbol="XAUUSD", tf=Timeframe.M5,
            overrides={"fast": "3", "slow": "5", "sl_ticks": "50"})
        assert provider._fast.period == 3
        assert provider._slow.period == 5
        assert provider._sl_ticks == 50

    def test_unknown_param_raises(self):
        with pytest.raises(ValueError, match="no parameter"):
            build_provider_instance(SmaCrossProvider, symbol="XAUUSD", tf=Timeframe.M5,
                                    overrides={"nonexistent_param": "1"})


# ---------------------------------------------------------------------------
# 結合: run_one / self_verify_baseline / sweep (market_tpsl + 合成データ)
# ---------------------------------------------------------------------------

def _run_baseline():
    return run_one(CANDLES, provider=SmaCrossProvider(symbol="XAUUSD", tf=Timeframe.M5),
                  execution_factory=EXECUTION_FACTORY, spec=GOLD, ai_client="none")


class TestRunOne:
    def test_produces_trades_and_report(self):
        report, trades = _run_baseline()
        assert len(report.trades) > 0
        assert len(trades) == len(report.trades)
        assert all("pnl_ts" in t and "exit_time" in t for t in trades)

    def test_deterministic_repeat(self):
        """同一入力→同一出力(決定論。CLAUDE.md 主要コマンド節)。"""
        r1, _ = _run_baseline()
        r2, _ = _run_baseline()
        assert r1.trades == r2.trades
        assert r1.profit_factor == r2.profit_factor


class TestSelfVerifyBaseline:
    def test_matching_golden_passes(self):
        report, _ = _run_baseline()
        golden_trades = len(report.trades)
        golden_pf = str(report.profit_factor)
        golden_dd = report.max_drawdown_tick_steps
        # 例外を出さず通ること自体が合格条件
        self_verify_baseline(
            CANDLES, provider_cls=SmaCrossProvider, execution_factory=EXECUTION_FACTORY,
            spec=GOLD, tf=Timeframe.M5, ai_client="none",
            golden_trades=golden_trades, golden_pf=golden_pf, golden_max_dd=golden_dd)

    def test_mismatched_golden_raises(self):
        with pytest.raises(SweepBaselineMismatch, match="trades"):
            self_verify_baseline(
                CANDLES, provider_cls=SmaCrossProvider, execution_factory=EXECUTION_FACTORY,
                spec=GOLD, tf=Timeframe.M5, ai_client="none",
                golden_trades=999999)

    def test_no_golden_specified_just_runs(self):
        report, _ = self_verify_baseline(
            CANDLES, provider_cls=SmaCrossProvider, execution_factory=EXECUTION_FACTORY,
            spec=GOLD, tf=Timeframe.M5, ai_client="none")
        assert len(report.trades) > 0


class TestSweep:
    def test_ofat_style_configs_return_results_with_annual_and_warnings(self):
        configs = [
            SweepConfig(label="sl_ticks=20", overrides={"sl_ticks": "20"}),
            SweepConfig(label="sl_ticks=40", overrides={"sl_ticks": "40"}),
            SweepConfig(label="sl_ticks=60", overrides={"sl_ticks": "60"}),
        ]
        results = sweep(CANDLES, provider_cls=SmaCrossProvider,
                        execution_factory=EXECUTION_FACTORY, spec=GOLD, tf=Timeframe.M5,
                        configs=configs, ai_client="none")
        assert [r.label for r in results] == ["sl_ticks=20", "sl_ticks=40", "sl_ticks=60"]
        for r in results:
            assert isinstance(r.annual, dict)
            assert isinstance(r.warnings, list)
            # 合成データは単一年に収まるため、黒字なら単年集中警告が自明に発火する
            # (定石6の閾値そのものの健全性チェックであり、本ツールの不具合ではない)。

    def test_different_overrides_change_results(self):
        """スイープ対象パラメータを変えれば結果(トレード数等)も変わる(no-op検出)。"""
        configs = [
            SweepConfig(label="tp=20", overrides={"tp_ticks": "20"}),
            SweepConfig(label="tp=1000", overrides={"tp_ticks": "1000"}),
        ]
        results = sweep(CANDLES, provider_cls=SmaCrossProvider,
                        execution_factory=EXECUTION_FACTORY, spec=GOLD, tf=Timeframe.M5,
                        configs=configs, ai_client="none")
        assert results[0].net_profit_ts != results[1].net_profit_ts

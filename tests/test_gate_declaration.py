"""T15: ゲートの手法宣言化 (docs/t15-gate-declaration-design.md)。

`TradingLoop` が全プランへゲート呼び出しを強制する構造を「手法がゲートの要否を
宣言する」形へ縮退させたことの検証。

  1. `resolve_ai_client` の優先順位 (明示CLI > strategy宣言 > レガシー既定"rule")
  2. `TradingLoop(gateway=None)`: judge を経ずにリスク層→発注へ進む /
     VERDICT がジャーナルに記録されない / new_day で例外にならない
  3. registry の `gate` 宣言値
  4. 統合: `--strategy atr_trend_scalp` フラグ無しで gateway=None が配線される
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from infers.ai.gateway import (
    AiGateway, EscalationPolicy, JudgementKind, JudgementRequest, Verdict, VerdictCache,
)
from infers.ai.passthrough import PASSTHROUGH_POLICY, PassthroughLlmClient
from infers.backtest.engine import BacktestEngine, LedgerBroker
from infers.core.loop import ProviderOutput, TradePlan, TradingLoop
from infers.core.models import Candle, Timeframe
from infers.execution.risk import RiskConfig, RiskManager
from infers.execution.sm import FsmConfig
from infers.main import _build_gateway, parse_args, resolve_ai_client
from infers.strategies.atr_trend_scalp.provider import AtrTrendScalpProvider
from infers.strategies.registry import get_strategy

UTC = timezone.utc
T0 = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
FAR = T0 + timedelta(days=30)


def mk_candle(i: int, h: int, l: int, c: int, tf: Timeframe = Timeframe.M5) -> Candle:
    o = max(l, min(h, c))
    return Candle(symbol="XAUUSD", tf=tf, open_time=T0 + i * tf.duration,
                  o_int=o, h_int=h, l_int=l, c_int=c, volume=1, is_closed=True)


def make_plan(plan_id: str = "p1") -> TradePlan:
    return TradePlan(
        plan_id=plan_id, direction=+1, limit_price_int=990, volume_steps=2,
        add_volume_steps=2, sl_int=960, expiry=FAR, invalidation_price=950,
        w1_high_int=1020, fib_target_int=1071,
        request=JudgementRequest(kind=JudgementKind.ENTRY_GATE, symbol="XAUUSD",
                                 direction=+1, features={"tag": "gatetest"}),
        cluster_score=Decimal("2.5"), ambiguity=Decimal("0.5"),
    )


class RaisingGateway:
    """gateway が誤って呼ばれたら即座に検出するためのスタブ。"""

    def judge(self, request, *, cluster_score, ambiguity):
        raise AssertionError("gateway.judge が gate=None 経路で呼ばれた")

    def new_day(self):
        raise AssertionError("gateway.new_day が gate=None 経路で呼ばれた")


class RecordingJournal:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def set_bar(self, bar_time):
        pass

    def record(self, kind, data):
        self.events.append((kind, data))

    def fsm_sink(self, position_id):
        return lambda kind, data: None


def make_loop(*, gateway, journal=None) -> TradingLoop:
    return TradingLoop(
        broker=LedgerBroker(spread_ticks=2, min_stop_distance_ticks=5),
        gateway=gateway,
        risk=RiskManager(RiskConfig(max_position_volume_steps=4,
            max_total_volume_steps=8, max_spread_ticks=10,
            daily_loss_limit_tick_steps=10_000)),
        fsm_config=FsmConfig(min_be_distance_ticks=10, be_offset_ticks=2,
            breakout_buffer_ticks=10),
        journal=journal,
    )


# ---------------------------------------------------------------------------
# 1. resolve_ai_client の優先順位テーブル
# ---------------------------------------------------------------------------

class TestResolveAiClient:
    def test_explicit_cli_wins_over_strategy_gate(self):
        """優先順位1: 明示 --ai-client は strategy 宣言(gate=None)より優先される。"""
        args = parse_args(["--mode", "backtest", "--strategy", "atr_trend_scalp",
                           "--ai-client", "none"])
        assert resolve_ai_client(args) == "none"

    def test_explicit_cli_rule_over_gate_none_strategy(self):
        args = parse_args(["--mode", "backtest", "--strategy", "market_tpsl",
                           "--ai-client", "rule"])
        assert resolve_ai_client(args) == "rule"

    def test_strategy_gate_rule_when_no_explicit_flag(self):
        """優先順位2: depth50/narrow_focus は gate='rule' を宣言 (--ai-client 省略時)。"""
        args = parse_args(["--mode", "backtest", "--strategy", "depth50"])
        assert resolve_ai_client(args) == "rule"

        args2 = parse_args(["--mode", "backtest", "--strategy", "narrow_focus"])
        assert resolve_ai_client(args2) == "rule"

    def test_strategy_gate_none_when_no_explicit_flag(self):
        """優先順位2: atr_trend_scalp/market_tpsl/smc_bos は gate=None を宣言。"""
        for name in ("atr_trend_scalp", "market_tpsl", "smc_bos"):
            args = parse_args(["--mode", "backtest", "--strategy", name])
            assert resolve_ai_client(args) is None, name

    def test_legacy_path_defaults_to_rule(self):
        """優先順位3: --strategy 無し(従来のフラグ組み立て経路)は 'rule'。"""
        args = parse_args(["--mode", "backtest"])
        assert resolve_ai_client(args) == "rule"

    def test_build_gateway_none_when_resolved_none(self):
        args = parse_args(["--mode", "backtest", "--strategy", "atr_trend_scalp",
                           "--verdict-cache", ":memory:"])
        assert _build_gateway(args) is None

    def test_build_gateway_rule_when_legacy(self):
        args = parse_args(["--mode", "backtest", "--verdict-cache", ":memory:"])
        gw = _build_gateway(args)
        assert gw is not None
        assert isinstance(gw, AiGateway)


# ---------------------------------------------------------------------------
# 2. TradingLoop(gateway=None)
# ---------------------------------------------------------------------------

class TestTradingLoopGatewayNone:
    def test_plan_skips_judge_and_reaches_order(self):
        """gateway=None: judge を経ずにリスク層→発注へ進む(=ポジションが開く)。"""
        loop = make_loop(gateway=None)
        candle = mk_candle(0, 1005, 985, 1000)
        output = ProviderOutput(plans=[make_plan("p1")])
        loop.on_candle(candle, output, spread_ticks=2)
        assert "p1" in loop.open_positions

    def test_no_verdict_recorded_in_journal(self):
        """gateway=None: VERDICT イベントがジャーナルに記録されない。"""
        journal = RecordingJournal()
        loop = make_loop(gateway=None, journal=journal)
        candle = mk_candle(0, 1005, 985, 1000)
        output = ProviderOutput(plans=[make_plan("p1")])
        loop.on_candle(candle, output, spread_ticks=2)
        kinds = [kind for kind, _ in journal.events]
        assert "VERDICT" not in kinds

    def test_new_day_no_exception_with_gateway_none(self):
        """gateway=None: 日次境界処理 (new_day) で例外にならない。"""
        loop = make_loop(gateway=None)
        day1 = mk_candle(0, 1005, 985, 1000)
        # 翌日の足 (M5 の1本では日を跨がないため意図的に別日を構築)
        day2 = Candle(symbol="XAUUSD", tf=Timeframe.M5,
                      open_time=T0 + timedelta(days=1),
                      o_int=1000, h_int=1005, l_int=985, c_int=1000,
                      volume=1, is_closed=True)
        loop.on_candle(day1, ProviderOutput(), spread_ticks=2)
        # 例外が飛ばないことそのものが検証対象
        loop.on_candle(day2, ProviderOutput(), spread_ticks=2)

    def test_gateway_never_called_when_none(self):
        """RaisingGateway で置換しても、gateway=None の場合は当然呼ばれない
        (対照実験: gateway に不正な値を渡さず None 経路が judge/new_day を
        一切参照しないことを別角度から確認する)。"""
        loop = make_loop(gateway=None)
        assert loop._gateway is None
        candle = mk_candle(0, 1005, 985, 1000)
        # 例外なく完走すれば良い (judge を呼べば AttributeError で即死する経路のため)
        loop.on_candle(candle, ProviderOutput(plans=[make_plan("p2")]), spread_ticks=2)


class TestTradingLoopGatewayPresent:
    def test_existing_gateway_path_unaffected(self):
        """gateway が非None のとき、既存の GO 判定経路が従来どおり動作する
        (呼び出し列不変の傍証。設計書 §3-3)。"""
        gw = AiGateway(
            client=_AlwaysGoClient(),
            cache=VerdictCache(":memory:"),
            policy=EscalationPolicy(score_l1=Decimal(2), score_l2=Decimal(4),
                                    ambiguity_gray=Decimal("0.1"),
                                    l2_daily_call_cap=1000),
        )
        journal = RecordingJournal()
        loop = make_loop(gateway=gw, journal=journal)
        candle = mk_candle(0, 1005, 985, 1000)
        loop.on_candle(candle, ProviderOutput(plans=[make_plan("p1")]), spread_ticks=2)
        assert "p1" in loop.open_positions
        kinds = [kind for kind, _ in journal.events]
        assert "VERDICT" in kinds


class _AlwaysGoClient:
    def judge(self, request, tier):
        return Verdict(decision="GO", confidence=Decimal("0.9"), reasons=["ok"], source="test")


# ---------------------------------------------------------------------------
# 3. registry の gate 宣言値
# ---------------------------------------------------------------------------

class TestRegistryGateDeclaration:
    @pytest.mark.parametrize("name", ["narrow_focus", "depth50"])
    def test_nf_family_declares_rule_gate(self, name):
        assert get_strategy(name).gate == "rule"

    @pytest.mark.parametrize("name", ["market_tpsl", "smc_bos", "atr_trend_scalp"])
    def test_non_nf_strategies_declare_no_gate(self, name):
        assert get_strategy(name).gate is None


# ---------------------------------------------------------------------------
# 5. 統合: gateway=None (新経路) と passthrough (旧経路) のトレード列一致
# ---------------------------------------------------------------------------

def _mk5(i: int, h: int, l: int, c: int, vol: int = 100) -> Candle:
    o = max(l, min(h, c))
    return Candle(symbol="XAUUSD", tf=Timeframe.M5, open_time=T0 + i * Timeframe.M5.duration,
                  o_int=o, h_int=h, l_int=l, c_int=c, volume=vol, is_closed=True)


def _atr_trend_candles() -> list[Candle]:
    candles = [_mk5(i, 1000 + 12 * i + 4, 1000 + 12 * i - 4, 1000 + 12 * i, 100)
               for i in range(9)]
    candles.append(_mk5(9, 1140, 980, 1135, 100))     # 参入トリガー
    candles.append(_mk5(10, 1400, 1300, 1390, 100))   # 大きく順行 → TP1/TP2 決済
    return candles


def _make_atr_provider() -> AtrTrendScalpProvider:
    return AtrTrendScalpProvider(
        symbol="XAUUSD", tf=Timeframe.M5, htf=Timeframe.M15,
        ema_fast_period=2, ema_medium_period=4, ema_trend_period=2,
        atr_period=2, vol_sma_period=2, atr_avg_period=2, retrace_lookback=3,
        atr_vol_mult=Decimal("0.5"), vol_mult=Decimal("0.5"),
        min_stop_distance_ticks=5, volume_steps=2)


def _run_atr_engine(*, gateway):
    engine = BacktestEngine(
        broker=LedgerBroker(spread_ticks=2, min_stop_distance_ticks=5),
        gateway=gateway,
        risk=RiskManager(RiskConfig(max_position_volume_steps=4, max_total_volume_steps=8,
                                    max_spread_ticks=10, daily_loss_limit_tick_steps=10_000_000)),
        fsm_config=FsmConfig(min_be_distance_ticks=10, be_offset_ticks=2,
                             breakout_buffer_ticks=10),
        execution_factory=get_strategy("atr_trend_scalp").build_execution,
    )
    return engine.run(_atr_trend_candles(), _make_atr_provider())


class TestGatewayNoneVsPassthroughParity:
    def test_atr_trend_scalp_trade_list_identical_new_vs_legacy_passthrough(self):
        """(a) --ai-client none 明示 (旧経路=パススルー) と
        (b) gateway=None (新経路=ゲート呼び出しなし) のトレード列が完全一致すること
        (完了ゲート§0-3)。ゲート統計行の有無は意図された差(検証対象外)。"""
        passthrough_gateway = AiGateway(client=PassthroughLlmClient(),
                                        cache=VerdictCache(), policy=PASSTHROUGH_POLICY)
        report_legacy = _run_atr_engine(gateway=passthrough_gateway)
        report_new = _run_atr_engine(gateway=None)

        assert len(report_legacy.trades) >= 1
        legacy_view = [(t.direction, t.pnl_tick_steps, t.exit_kind) for t in report_legacy.trades]
        new_view = [(t.direction, t.pnl_tick_steps, t.exit_kind) for t in report_new.trades]
        assert legacy_view == new_view
        assert report_legacy.total_pnl_tick_steps == report_new.total_pnl_tick_steps


class TestCliWiringGatewayNone:
    def test_strategy_atr_trend_scalp_without_ai_client_flag_wires_none(self):
        """`--strategy atr_trend_scalp` をフラグ無しで指定すると
        `_build_gateway` 経由で gateway=None が配線される (main.py 統合)。"""
        args = parse_args(["--mode", "backtest", "--strategy", "atr_trend_scalp",
                           "--symbol", "XAUUSD", "--tf", "M5",
                           "--verdict-cache", ":memory:"])
        assert _build_gateway(args) is None

    def test_strategy_depth50_without_ai_client_flag_wires_rule_gateway(self):
        """`--strategy depth50` はフラグ無しでも 'rule' ゲートが配線される
        (後方互換: bit-check 対象コマンドの経路)。"""
        args = parse_args(["--mode", "backtest", "--strategy", "depth50",
                           "--verdict-cache", ":memory:"])
        gw = _build_gateway(args)
        assert gw is not None and isinstance(gw, AiGateway)

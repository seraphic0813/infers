"""market_tpsl 手法 (段階2.5): 成行+固定TP/SL 執行モデルと、TradingLoop が
Narrow Focus とは別の執行ライフサイクルを同一コードパスで駆動できることの検証。"""

from datetime import datetime, timezone
from decimal import Decimal

from infers.ai.gateway import Verdict
from infers.backtest.engine import BacktestEngine, LedgerBroker
from infers.core.models import Candle, Timeframe
from infers.execution.risk import RiskConfig, RiskManager
from infers.execution.sim_broker import SimBroker
from infers.strategies.market_tpsl.execution import MarketState, MarketTpSlExecution
from infers.strategies.market_tpsl.provider import SmaCrossProvider
from infers.strategies.narrow_focus.execution import FsmConfig
from infers.strategies.registry import get_strategy, strategy_names

UTC = timezone.utc
T0 = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


def mk(i: int, h: int, l: int, c: int) -> Candle:
    o = max(l, min(h, c))
    return Candle(symbol="XAUUSD", tf=Timeframe.M5,
                  open_time=T0 + i * Timeframe.M5.duration,
                  o_int=o, h_int=h, l_int=l, c_int=c, volume=1, is_closed=True)


class _Intent:
    """TradePlan 互換の最小エントリー意図 (place が読むフィールドのみ)。"""
    def __init__(self, *, entry: int, sl: int, tp: int, vol: int = 2):
        self.limit_price_int = entry
        self.sl_int = sl
        self.fib_target_int = tp
        self.volume_steps = vol


# ---------------------------------------------------------------------------
# 1. 執行モデル単体: 成行参入 → TP / SL / 強制手仕舞い
# ---------------------------------------------------------------------------

class TestMarketTpSlExecution:
    def _broker(self) -> SimBroker:
        b = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        b.process_bar(mk(0, 1005, 995, 1000))   # last_close を用意
        return b

    def test_market_entry_then_tp_close(self):
        broker = self._broker()
        ex = MarketTpSlExecution(position_id="m1", direction=+1, broker=broker)
        ex.place(_Intent(entry=1000, sl=980, tp=1040))
        assert ex.state is MarketState.OPEN
        assert ex.entry_price_int == 1002          # last_close 1000 + spread 2

        bar1 = mk(1, 1030, 1010, 1025)             # 高値1030 < TP1040 → 未到達
        broker.process_bar(bar1)
        assert ex.on_bar(bar1, None).closed is False

        bar2 = mk(2, 1045, 1020, 1042)             # 高値1045 >= TP1040 → 全決済
        broker.process_bar(bar2)
        out = ex.on_bar(bar2, None)
        assert out.closed and ex.state is MarketState.CLOSED
        assert ex.volume_steps == 0
        assert ex.journal[-1][0] == "TP_CLOSE"

    def test_market_entry_then_sl_hit(self):
        broker = self._broker()
        ex = MarketTpSlExecution(position_id="m2", direction=+1, broker=broker)
        ex.place(_Intent(entry=1000, sl=980, tp=1040))

        bar1 = mk(1, 1000, 975, 985)               # 安値975 <= SL980 → SLヒット
        events = broker.process_bar(bar1)
        assert any(e.kind == "SL_HIT" for e in events)
        for e in events:
            ex.on_broker_event(e)
        assert ex.closed and ex.state is MarketState.CLOSED
        assert ex.journal[-1][0] == "SL_HIT"

    def test_short_direction_tp(self):
        broker = self._broker()
        ex = MarketTpSlExecution(position_id="m3", direction=-1, broker=broker)
        ex.place(_Intent(entry=1000, sl=1020, tp=960))   # 売り: TPは下
        assert ex.state is MarketState.OPEN
        bar = mk(1, 990, 955, 965)                       # 安値955 <= TP960 → 全決済
        broker.process_bar(bar)
        assert ex.on_bar(bar, None).closed
        assert ex.journal[-1][0] == "TP_CLOSE"

    def test_close_forces_flat(self):
        broker = self._broker()
        ex = MarketTpSlExecution(position_id="m4", direction=+1, broker=broker)
        ex.place(_Intent(entry=1000, sl=980, tp=1040))
        ex.close("END_OF_DATA")
        assert ex.closed and ex.volume_steps == 0
        assert ex.journal[-1][0] == "CLOSE_ALL"


# ---------------------------------------------------------------------------
# 2. レジストリ配線: market_tpsl は別執行モデルを持つ / 既存手法は None (既定)
# ---------------------------------------------------------------------------

class TestRegistryWiring:
    def test_market_tpsl_registered_with_execution(self):
        assert "market_tpsl" in strategy_names()
        assert get_strategy("market_tpsl").build_execution is not None

    def test_narrow_focus_strategies_use_default_execution(self):
        # depth50/narrow_focus は build_execution=None → TradingLoop 既定 (Narrow Focus)
        assert get_strategy("depth50").build_execution is None
        assert get_strategy("narrow_focus").build_execution is None


# ---------------------------------------------------------------------------
# 3. 結合: BacktestEngine + 別執行モデルを TradingLoop が駆動 (抽象の実証)
# ---------------------------------------------------------------------------

class _GoGateway:
    """寛容ゲートウェイ (常に GO)。AIゲートは Narrow Focus 固有のため、別手法の
    検証では決定論で GO を返す最小スタブを使う。"""
    def judge(self, request, *, cluster_score, ambiguity):  # noqa: ARG002
        return Verdict(decision="GO", confidence=Decimal("0.9"),
                       reasons=["market_tpsl test"], source="POLICY")
    def new_day(self) -> None: ...


def test_engine_drives_market_execution_end_to_end():
    """SMAゴールデンクロスで成行参入し、次足でTP到達して1トレードが確定する。
    BacktestEngine→TradingLoop→MarketTpSlExecution の経路が成立することを示す。"""
    # 終値 1000,1000,1000,1010,1055。fast=SMA2 / slow=SMA3。
    #   c2: diff=0 (prev符号=0) → c3: fast1005>slow1003.3 → ゴールデンクロス → 買い
    candles = [
        mk(0, 1005, 995, 1000),
        mk(1, 1005, 995, 1000),
        mk(2, 1005, 995, 1000),
        mk(3, 1015, 1005, 1010),     # クロス → entry=1010, sl=990, tp=1050
        mk(4, 1060, 1015, 1055),     # 高値1060 >= TP1050 → 決済
    ]
    provider = SmaCrossProvider(symbol="XAUUSD", tf=Timeframe.M5,
                                fast=2, slow=3, sl_ticks=20, tp_ticks=40, volume_steps=2)
    engine = BacktestEngine(
        broker=LedgerBroker(spread_ticks=2, min_stop_distance_ticks=5),
        gateway=_GoGateway(),
        risk=RiskManager(RiskConfig(max_position_volume_steps=4, max_total_volume_steps=8,
                                    max_spread_ticks=10, daily_loss_limit_tick_steps=10_000_000)),
        fsm_config=FsmConfig(min_be_distance_ticks=10, be_offset_ticks=2,
                             breakout_buffer_ticks=10),
        execution_factory=get_strategy("market_tpsl").build_execution,  # レジストリ経由
    )
    report = engine.run(candles, provider)

    assert len(report.trades) == 1
    trade = report.trades[0]
    assert trade.direction == +1
    assert trade.pnl_tick_steps > 0          # TP決済の利益 (SLなら負)
    assert trade.exit_kind == "CLOSE"        # TPは close_volume 経由 (SLヒットではない)

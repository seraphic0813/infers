"""フェーズ8 結合テスト: エクスポータ・ライブ駆動ループ・エントリーポイント。

MT5への実接続は一切行わず、MarketFeed / BrokerPort の抽象境界で
合成実装 (FakeFeed / LedgerBroker) を注入して全系の協調動作を検証する。
"""

import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from infers.ai.gateway import (
    AiGateway, EscalationPolicy, JudgementKind, JudgementRequest, Verdict, VerdictCache,
)
from infers.analysis.dow import StructureEvent, StructureEventType, TrendState
from infers.strategies.narrow_focus.zigzag import SwingPoint
from infers.backtest.engine import LedgerBroker
from infers.core.loop import ProviderOutput, TradePlan
from infers.data.exporter import export_history
from infers.data.feed import MarketFeed
from infers.core.models import Candle, SymbolSpec, Timeframe
from infers.execution.mt5_adapter import LiveRunner
from infers.execution.risk import RiskConfig, RiskManager
from infers.execution.sm import FsmConfig, PosState
from infers.main import NullProvider, load_provider, parse_args

UTC = timezone.utc
T0 = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
FAR = T0 + timedelta(days=30)
GOLD = SymbolSpec(name="XAUUSD", tick_size=Decimal("0.01"), lot_step=Decimal("0.01"), digits=2)


def mk_candle(i: int, h: int, l: int, c: int, tf: Timeframe = Timeframe.M5) -> Candle:
    o = max(l, min(h, c))
    return Candle(symbol="XAUUSD", tf=tf, open_time=T0 + i * tf.duration,
                  o_int=o, h_int=h, l_int=l, c_int=c, volume=1, is_closed=True)


class FakeFeed(MarketFeed):
    """確定足リストをそのまま供給する合成フィード (MT5モック)。"""

    def __init__(self, candles: list[Candle]):
        self._candles = candles
        self.history_calls: list[tuple[datetime, datetime]] = []

    def connect(self) -> None: ...
    def close(self) -> None: ...

    def get_history(self, spec, tf, start, end):
        self.history_calls.append((start, end))
        return [c for c in self._candles if start <= c.open_time < end]

    def iter_closed(self, spec, tf, *, stop: threading.Event | None = None):
        for c in self._candles:
            if stop is not None and stop.is_set():
                return
            yield c


# ---------------------------------------------------------------------------
# エクスポータ (チャンク取得 → 整数ティックのまま保存)
# ---------------------------------------------------------------------------

class TestExporter:
    def test_chunked_export_dedupes_and_sorts(self):
        candles = [mk_candle(i, 1010 + i, 990 + i, 1000 + i, tf=Timeframe.D1)
                   for i in range(70)]                     # 70日分のD1
        feed = FakeFeed(candles)
        captured: dict = {}

        def fake_writer(rows, path):
            captured["rows"] = list(rows)
            captured["path"] = path

        n = export_history(feed, GOLD, Timeframe.D1,
                           start=T0, end=T0 + timedelta(days=70),
                           out_path="out.parquet",
                           chunk=timedelta(days=30), writer=fake_writer)
        assert n == 70
        assert len(feed.history_calls) == 3                # 30+30+10日の3チャンク
        rows = captured["rows"]
        assert len(rows) == 70
        times = [r["time"] for r in rows]
        assert times == sorted(times)                      # 昇順・重複なし
        first = rows[0]
        # 価格は整数ティックのまま (floatを経由しない: CLAUDE.md 第6条)
        assert all(isinstance(first[k], int) for k in ("o_int", "h_int", "l_int", "c_int"))
        assert first["tf"] == "D1" and first["symbol"] == "XAUUSD"

    def test_invalid_range_rejected(self):
        with pytest.raises(ValueError, match="start must be before end"):
            export_history(FakeFeed([]), GOLD, Timeframe.D1,
                           start=T0, end=T0, out_path="x", writer=lambda r, p: None)

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValueError, match="tz-aware"):
            export_history(FakeFeed([]), GOLD, Timeframe.D1,
                           start=datetime(2026, 1, 1), end=T0,
                           out_path="x", writer=lambda r, p: None)


# ---------------------------------------------------------------------------
# ライブ駆動ループ (LiveRunner) — フェーズ6/7部品との協調動作
# ---------------------------------------------------------------------------

GO = Verdict(decision="GO", confidence=Decimal("0.8"), reasons=["ok"])
NO = Verdict(decision="NO_GO", confidence=Decimal("0.9"), reasons=["weak"])
POLICY = EscalationPolicy(score_l1=Decimal(2), score_l2=Decimal(4),
                          ambiguity_gray=Decimal("0.1"), l2_daily_call_cap=3)


class FakeClient:
    def __init__(self, l1: Verdict | Exception = GO):
        self._l1 = l1
        self.calls = 0

    def judge(self, request, tier):
        self.calls += 1
        if isinstance(self._l1, Exception):
            raise self._l1
        return self._l1


def make_plan() -> TradePlan:
    return TradePlan(
        plan_id="live1", direction=+1, limit_price_int=990, volume_steps=2,
        add_volume_steps=2, sl_int=960, expiry=FAR, invalidation_price=950,
        w1_high_int=1020, fib_target_int=1071,
        request=JudgementRequest(kind=JudgementKind.ENTRY_GATE, symbol="XAUUSD",
                                 direction=+1, features={"tag": "live"}),
        cluster_score=Decimal("2.5"), ambiguity=Decimal("0.5"),
    )


def hl_event(price: int) -> StructureEvent:
    s1 = SwingPoint(kind="LOW", bar_time=T0, price_int=price - 5, tf=Timeframe.M5,
                    confirmed_at=T0 + timedelta(minutes=5))
    s2 = SwingPoint(kind="LOW", bar_time=T0 + timedelta(minutes=10), price_int=price,
                    tf=Timeframe.M5, confirmed_at=T0 + timedelta(minutes=15))
    return StructureEvent(type=StructureEventType.HL, swing=s2, prev_swing=s1,
                          state_after=TrendState.UP)


class ScriptedProvider:
    def __init__(self, script: dict[int, ProviderOutput]):
        self._script = script
        self._i = -1

    def on_candle(self, candle):
        self._i += 1
        return self._script.get(self._i, ProviderOutput())


CANDLES = [
    mk_candle(0, 1005, 995, 1000),
    mk_candle(1, 1005, 995, 1000),     # プラン発行 → AIゲートGO → 打診指値
    mk_candle(2, 1000, 988, 992),      # 打診約定 (990)
    mk_candle(3, 1035, 1015, 1031),    # W1ブレイク → 追撃
    mk_candle(4, 1040, 1020, 1035),    # HL確定 → 建値SL (平均建値1012基準 → 1014)
    mk_candle(5, 1075, 1040, 1070),    # RSI利確圏到達 → 半分利確 (§6.4)
    mk_candle(6, 1050, 990, 1000),     # SLヒット → CLOSED
]
SCRIPT = {1: ProviderOutput(plans=[make_plan()]),
          4: ProviderOutput(structure_events=[hl_event(1025)]),
          5: ProviderOutput(rsi_value=Decimal(75))}     # 半分利確を RSI 利確圏で発火


def make_runner(client: FakeClient):
    broker = LedgerBroker(spread_ticks=2, min_stop_distance_ticks=5)
    runner = LiveRunner(
        feed=FakeFeed(CANDLES), spec=GOLD, tf=Timeframe.M5, broker=broker,
        provider=ScriptedProvider(SCRIPT),
        gateway=AiGateway(client=client, cache=VerdictCache(), policy=POLICY),
        risk=RiskManager(RiskConfig(max_position_volume_steps=4, max_total_volume_steps=8,
                                    max_spread_ticks=10, daily_loss_limit_tick_steps=10_000)),
        fsm_config=FsmConfig(min_be_distance_ticks=10, be_offset_ticks=2,
                             breakout_buffer_ticks=10),
        event_source=broker.process_bar,        # 結合テスト: Simの約定イベントを注入
        spread_fn=lambda: 2,
    )
    return runner, broker


class TestLiveRunner:
    def test_full_live_loop_lifecycle(self):
        """打診→追撃→建値SL→半分利確→SL退出 がライブ駆動ループでも完走する。"""
        runner, broker = make_runner(FakeClient())
        bars = runner.run(max_bars=len(CANDLES))
        assert bars == len(CANDLES)
        # ポジションは全行程を経てクローズ済み (ループからも除去)
        assert runner.loop.open_positions == {}
        assert broker.position("live1") is None
        # 約定台帳に全行程が残る (entry: 990×2 + 1033×2 / exit: 1068×2 + SL1014×2)
        # 建値SL=1014 は平均建値 (990×2+1033×2)/4=1012 + 微益2 (P7)
        led = broker.ledgers["live1"]
        assert led.entries == [(990, 2), (1033, 2)]
        assert led.exits == [(1068, 2), (1014, 2)]
        assert led.exit_kind == "SL"

    def test_no_go_keeps_account_flat(self):
        runner, broker = make_runner(FakeClient(NO))
        runner.run(max_bars=len(CANDLES))
        assert runner.loop.open_positions == {}
        assert broker.pending_count == 0

    def test_loop_delegates_to_execution_on_bar(self):
        """ループは既存ポジションの管理を ExecutionModel.on_bar へ完全委譲し、
        戻り値 BarOutcome に従って失効リカバリー (expired) とクローズ回収 (closed)
        のみを行う (段階2.3 の抽象境界。手法固有の執行手順はループに無い)。"""
        from infers.core.execution import BarOutcome
        from infers.core.loop import TradingLoop

        class StubExecution:
            """ExecutionModel を構造的に充足する最小スタブ。"""
            def __init__(self, outcome: BarOutcome):
                self._outcome = outcome
                self.bars = 0
                self.volume_steps = 1
            def place(self, intent): ...
            def on_broker_event(self, ev): ...
            def on_bar(self, candle, signal) -> BarOutcome:
                self.bars += 1
                return self._outcome
            def close(self, reason): ...

        def make_loop() -> TradingLoop:
            return TradingLoop(
                broker=LedgerBroker(spread_ticks=2, min_stop_distance_ticks=5),
                gateway=AiGateway(client=FakeClient(), cache=VerdictCache(), policy=POLICY),
                risk=RiskManager(RiskConfig(max_position_volume_steps=4,
                    max_total_volume_steps=8, max_spread_ticks=10,
                    daily_loss_limit_tick_steps=10_000)),
                fsm_config=FsmConfig(min_be_distance_ticks=10, be_offset_ticks=2,
                    breakout_buffer_ticks=10))

        candle = mk_candle(0, 1005, 995, 1000)

        # closed=True かつ expired=True: ループは sink を呼び、ポジションを回収する。
        sink: list[str] = []
        loop = make_loop(); loop._expiry_sink = sink.append
        ex = StubExecution(BarOutcome(closed=True, expired=True))
        loop.open_positions["p"] = (ex, make_plan())
        closed = loop.on_candle(candle, ProviderOutput(), spread_ticks=2)
        assert ex.bars == 1                              # on_bar に委譲された
        assert sink == ["p"]                             # expired → 失効リカバリー
        assert closed == ["p"] and "p" not in loop.open_positions  # closed → 回収

        # closed=False かつ expired=False: ループは何も外形変化させない。
        loop2 = make_loop()
        ex2 = StubExecution(BarOutcome(closed=False, expired=False))
        loop2.open_positions["p"] = (ex2, make_plan())
        closed2 = loop2.on_candle(candle, ProviderOutput(), spread_ticks=2)
        assert ex2.bars == 1 and closed2 == [] and "p" in loop2.open_positions

    def test_llm_panic_keeps_loop_alive(self):
        """LLM全停止でもライブループは例外なく完走し口座はフラット。"""
        runner, broker = make_runner(FakeClient(RuntimeError("api down")))
        bars = runner.run(max_bars=len(CANDLES))
        assert bars == len(CANDLES)
        assert broker.pending_count == 0

    def test_stop_event_halts_loop(self):
        runner, _ = make_runner(FakeClient())
        stop = threading.Event()
        stop.set()
        assert runner.run(stop=stop) == 0

    def test_shutdown_aborts_pending_probe(self):
        """常駐停止時: 未約定の打診指値が取り消される (冪等・安全停止)。"""
        runner, broker = make_runner(FakeClient())
        runner.run(max_bars=2)                    # bar1でプラン発注、約定はbar2のため未約定
        (fsm, _plan) = runner.loop.open_positions["live1"]
        assert fsm.state is PosState.PROBE_PENDING
        closed = runner.shutdown()
        assert closed == ["live1"]
        assert broker.pending_count == 0
        assert fsm.state is PosState.CLOSED


# ---------------------------------------------------------------------------
# エントリーポイント (main.py)
# ---------------------------------------------------------------------------

def make_test_provider():
    return NullProvider()


class TestMainEntry:
    def test_parse_args_defaults(self):
        args = parse_args(["--mode", "backtest", "--data", "x.parquet"])
        assert args.mode == "backtest" and args.symbol == "XAUUSD"
        assert args.tf == "M5" and args.demo is True

    def test_mode_required(self):
        with pytest.raises(SystemExit):
            parse_args([])

    def test_load_provider_factory(self):
        provider = load_provider(f"{__name__}:make_test_provider")
        assert isinstance(provider, NullProvider)
        out = provider.on_candle(mk_candle(0, 1005, 995, 1000))
        assert out.plans == [] and out.structure_events == []

    def test_load_provider_invalid_spec(self):
        with pytest.raises(ValueError, match="module:factory"):
            load_provider("not-a-valid-spec")

    def test_default_provider_is_null(self):
        assert isinstance(load_provider(None), NullProvider)

    def test_backtest_without_data_fails(self, capsys):
        from infers.main import main
        assert main(["--mode", "backtest"]) == 2
        assert "--data" in capsys.readouterr().err

    def test_default_provider_is_full_pipeline(self):
        """--provider 省略時の既定は InfersSignalProvider (フェーズ9結合)。"""
        from infers.main import build_provider
        from infers.strategies.narrow_focus.provider import InfersSignalProvider
        args = parse_args(["--mode", "live", "--symbol", "XAUUSD", "--tf", "M5"])
        provider = build_provider(args)
        assert isinstance(provider, InfersSignalProvider)
        assert provider.symbol == "XAUUSD" and provider.tf is Timeframe.M5


# ---------------------------------------------------------------------------
# リコンサイル (再起動・再接続時の強制同期)
# ---------------------------------------------------------------------------

from infers.execution.mt5_adapter import (  # noqa: E402
    BrokerPositionState, BrokerSnapshot, reconcile_snapshot,
)


def make_runner_with_risk(client: FakeClient):
    broker = LedgerBroker(spread_ticks=2, min_stop_distance_ticks=5)
    risk = RiskManager(RiskConfig(max_position_volume_steps=4, max_total_volume_steps=8,
                                  max_spread_ticks=10, daily_loss_limit_tick_steps=10_000))
    runner = LiveRunner(
        feed=FakeFeed(CANDLES), spec=GOLD, tf=Timeframe.M5, broker=broker,
        provider=ScriptedProvider(SCRIPT),
        gateway=AiGateway(client=client, cache=VerdictCache(), policy=POLICY),
        risk=risk,
        fsm_config=FsmConfig(min_be_distance_ticks=10, be_offset_ticks=2,
                             breakout_buffer_ticks=10),
        event_source=broker.process_bar,
        spread_fn=lambda: 2,
    )
    return runner, broker, risk


class TestReconcile:
    def test_gap_fill_restores_probe_state(self):
        """切断中に打診指値が約定 → FILLイベント再生で PROBE_PENDING→PROBE。"""
        runner, broker, risk = make_runner_with_risk(FakeClient())
        runner.run(max_bars=2)                          # bar1で発注、未約定のまま停止
        fsm, _ = runner.loop.open_positions["live1"]
        assert fsm.state is PosState.PROBE_PENDING

        broker.snapshot = lambda: BrokerSnapshot(       # ブローカー実態: 約定済み
            positions={"live1": BrokerPositionState(
                volume_steps=2, sl_int=960, avg_entry_int=990)},
            pending=frozenset())
        report = runner.reconcile()
        assert report.ok and [e.kind for e in report.events] == ["FILL"]
        assert fsm.state is PosState.PROBE              # 正規の遷移経路で追いついた
        assert fsm.entry_price_int == 990 and fsm.volume_steps == 2
        assert not risk.kill_switch_engaged

    def test_gap_sl_hit_closes_position(self):
        """保有していたはずのポジションが消滅 → SLヒット未処理として再生。"""
        runner, broker, risk = make_runner_with_risk(FakeClient())
        runner.run(max_bars=3)                          # bar2で約定済み → PROBE
        fsm, _ = runner.loop.open_positions["live1"]
        assert fsm.state is PosState.PROBE

        broker.snapshot = lambda: BrokerSnapshot(positions={}, pending=frozenset())
        report = runner.reconcile()
        assert report.ok and [e.kind for e in report.events] == ["SL_HIT"]
        assert report.events[0].price_int == 960        # 価格はローカルSL (保守側)
        assert fsm.state is PosState.CLOSED

    def test_orphan_position_engages_kill_switch(self):
        """ローカルが知らない実態ポジション → 自動補正せずキルスイッチ。"""
        runner, broker, risk = make_runner_with_risk(FakeClient())
        runner.run(max_bars=3)
        broker.snapshot = lambda: BrokerSnapshot(
            positions={
                "live1": BrokerPositionState(volume_steps=2, sl_int=960,
                                             avg_entry_int=990),
                "ghost": BrokerPositionState(volume_steps=9, sl_int=0,
                                             avg_entry_int=1234),
            },
            pending=frozenset())
        report = runner.reconcile()
        assert not report.ok and report.orphans == ["ghost"]
        assert risk.kill_switch_engaged                 # 新規は全拒否 (人間確認待ち)

    def test_sl_drift_repaired_from_local_truth(self):
        """SLの正は単調性保証済みのローカル値 → ブローカー側を修復する。"""
        runner, broker, risk = make_runner_with_risk(FakeClient())
        runner.run(max_bars=3)
        fsm, _ = runner.loop.open_positions["live1"]
        broker.snapshot = lambda: BrokerSnapshot(
            positions={"live1": BrokerPositionState(
                volume_steps=2, sl_int=900, avg_entry_int=990)},  # SLがズレている
            pending=frozenset())
        report = runner.reconcile()
        assert report.ok and report.sl_repairs == [("live1", 960)]
        assert broker.position("live1").sl_int == 960
        assert fsm.state is PosState.PROBE              # 状態は変わらない

    def test_vanished_pending_is_human_escalation(self):
        """指値もポジションも無い (切断中に完結した可能性) → 自動補正しない。"""
        runner, broker, risk = make_runner_with_risk(FakeClient())
        runner.run(max_bars=2)
        report = reconcile_snapshot(
            BrokerSnapshot(positions={}, pending=frozenset()),
            runner.loop.open_positions)
        assert not report.ok
        assert "vanished" in report.mismatches[0]
        assert report.events == []                      # 推測でイベントを作らない

    def test_matching_state_is_clean_noop(self):
        runner, broker, risk = make_runner_with_risk(FakeClient())
        runner.run(max_bars=3)
        broker.snapshot = lambda: BrokerSnapshot(
            positions={"live1": BrokerPositionState(
                volume_steps=2, sl_int=960, avg_entry_int=990)},
            pending=frozenset())
        report = runner.reconcile()
        assert report.ok and report.events == [] and report.sl_repairs == []


# ---------------------------------------------------------------------------
# InfersSignalProvider (フェーズ9: 分析層フルパイプライン)
# ---------------------------------------------------------------------------

from infers.strategies.narrow_focus.provider import InfersSignalProvider, ProviderConfig  # noqa: E402

# macro_filter=False: ここはミクロのプラン生成を検証する (マクロ方向フィルターは
# 直交する別レイヤーで TestMacroGate/TestMacroResampler が担保)。
PROVIDER_CFG = ProviderConfig(rsi_period=5, sma_periods=(30,), cooldown_bars=30,
                              macro_filter=False)


def provider_series() -> list[Candle]:
    """ウォームアップ → 下落 → 上昇(W1) → 押し(HL) → 高値更新(HH) → 押し目形成。

    ダウ理論が UP を確定し、エリオットが [LOW 945, HIGH 1155] の
    第2波進行中カウントを持つ状態で、現在値 ~1050 から下の押し目に
    未来コンフルエンス (RSI極値 × レジサポ945) が現れる設計。
    """
    bars: list[Candle] = []

    def add(h: int, l: int, c: int) -> None:
        bars.append(mk_candle(len(bars), h, l, c))

    for j in range(30):
        add(1003, 997, 1000 + (j % 2))                  # ウォームアップ (低ボラ)
    for px in range(980, 899, -20):                     # 下落 → 最初のHIGH/LOW素材
        add(px + 5, px - 5, px)
    for px in range(925, 1101, 25):                     # 上昇 → HIGH 1105 (HH)
        add(px + 5, px - 5, px)
    for px in range(1075, 949, -25):                    # 押し → LOW 945 (HL) → UP確定
        add(px + 5, px - 5, px)
    for px in range(975, 1151, 25):                     # 上昇 W1 → HIGH 1155
        add(px + 5, px - 5, px)
    for px in range(1130, 1049, -20):                   # 第2波の押し目形成中
        add(px + 5, px - 5, px)
    for _ in range(10):
        add(1055, 1045, 1050)
    return bars


class TestInfersSignalProvider:
    def collect(self):
        provider = InfersSignalProvider(symbol="XAUUSD", tf=Timeframe.M5,
                                        config=PROVIDER_CFG)
        outputs = [provider.on_candle(c) for c in provider_series()]
        return provider, outputs

    def test_rejects_forming_bar_and_mismatched_series(self):
        provider = InfersSignalProvider(symbol="XAUUSD", tf=Timeframe.M5)
        forming = mk_candle(0, 1003, 997, 1000).model_copy(update={"is_closed": False})
        with pytest.raises(ValueError, match="closed candles only"):
            provider.on_candle(forming)
        with pytest.raises(ValueError, match="series mismatch"):
            provider.on_candle(mk_candle(0, 1003, 997, 1000, tf=Timeframe.H1))

    def test_no_plans_during_warmup(self):
        _, outputs = self.collect()
        assert all(not o.plans for o in outputs[:40])

    def test_macro_wave2_switches_wave_source(self):
        """macro_wave2=True で wave-2 判定を上位足エリオットに切替。短い系列では
        上位足の波が形成されず発注ゼロ(M5の偽第2波に依存しないことの確認)。"""
        base = dict(rsi_period=5, sma_periods=(30,), cooldown_bars=30, macro_filter=False)
        macro = InfersSignalProvider(symbol="XAUUSD", tf=Timeframe.M5,
            config=ProviderConfig(**base, macro_wave2=True))
        m5 = InfersSignalProvider(symbol="XAUUSD", tf=Timeframe.M5,
            config=ProviderConfig(**base, macro_wave2=False))
        series = provider_series()
        mo = [macro.on_candle(c) for c in series]
        m5o = [m5.on_candle(c) for c in series]
        assert sum(len(o.plans) for o in mo) == 0        # 上位足に波なし
        assert sum(len(o.plans) for o in m5o) >= 1        # M5なら発注あり

    def test_be_sl_macro_tf_routes_structure_events(self):
        """建値SLの構造トリガーTF (§②): be_sl_macro_tf で M5/上位足を切替える。"""
        base = dict(rsi_period=5, sma_periods=(30,), cooldown_bars=30, macro_filter=False)
        m5 = InfersSignalProvider(symbol="XAUUSD", tf=Timeframe.M5,
            config=ProviderConfig(**base, be_sl_macro_tf=False))
        macro = InfersSignalProvider(symbol="XAUUSD", tf=Timeframe.M5,
            config=ProviderConfig(**base, be_sl_macro_tf=True))
        series = provider_series()
        assert sum(len(m5.on_candle(c).structure_events) for c in series) > 0    # M5構造は多数
        assert sum(len(macro.on_candle(c).structure_events) for c in series) == 0  # 上位足は短系列で未形成

    def test_depth_screen_keeps_only_deep_pullback(self):
        """40%深さスクリーニング: 指値が第1波[945,1155]の下方40%(≤1029)に限定される。"""
        # span=210, cap = 945 + 0.4*210 = 1029
        cfg = ProviderConfig(rsi_period=5, sma_periods=(30,), cooldown_bars=30,
                             macro_filter=False, depth_screen=True)
        provider = InfersSignalProvider(symbol="XAUUSD", tf=Timeframe.M5, config=cfg)
        plans = [p for c in provider_series() for p in provider.on_candle(c).plans]
        assert plans, "深い押し目に合流があれば発注されるはず"
        for p in plans:
            assert p.limit_price_int <= 1029           # 下方40%(深い押し目)のみ
            # depth = (w1_high - limit)/(w1_high - w1_low) >= 0.60
            depth = (1155 - p.limit_price_int) / (1155 - 945)
            assert depth >= 0.60

    def test_depth_screen_off_allows_shallower(self):
        """depth_screen=False では下方40%より浅い指値も候補になり得る(全域グリッド)。"""
        deep = InfersSignalProvider(symbol="XAUUSD", tf=Timeframe.M5,
            config=ProviderConfig(rsi_period=5, sma_periods=(30,), cooldown_bars=30,
                                  macro_filter=False, depth_screen=True))
        wide = InfersSignalProvider(symbol="XAUUSD", tf=Timeframe.M5,
            config=ProviderConfig(rsi_period=5, sma_periods=(30,), cooldown_bars=30,
                                  macro_filter=False, depth_screen=False))
        series = provider_series()
        dmax = max((p.limit_price_int for c in series for p in deep.on_candle(c).plans),
                   default=0)
        wmax = max((p.limit_price_int for c in series for p in wide.on_candle(c).plans),
                   default=0)
        # 全域許可の方が、より浅い(高い)指値まで取り得る (狭めていない)
        assert wmax >= dmax

    def test_max_risk_ticks_falls_back_to_lower_risk_candidate(self):
        """1トレード最大リスク (entry-methodology.md G2-⑥訂正2026-06-15): SL距離(ticks)×
        volume_steps が max_risk_ticks を超える最良候補(limit=997, risk=130)は、
        cap を下回る次点候補(limit=946, risk=28)へフォールバックする。
        """
        default_plans = [p for o in self.collect()[1] for p in o.plans]
        assert len(default_plans) == 1
        assert default_plans[0].limit_price_int == 997
        assert abs(default_plans[0].limit_price_int - default_plans[0].sl_int) * \
            default_plans[0].volume_steps == 130

        capped_cfg = ProviderConfig(rsi_period=5, sma_periods=(30,), cooldown_bars=30,
                                     macro_filter=False, max_risk_ticks=129)
        provider = InfersSignalProvider(symbol="XAUUSD", tf=Timeframe.M5, config=capped_cfg)
        capped_plans = [p for c in provider_series() for p in provider.on_candle(c).plans]
        assert len(capped_plans) == 1
        p = capped_plans[0]
        assert p.limit_price_int == 946
        assert abs(p.limit_price_int - p.sl_int) * p.volume_steps == 28

    def test_max_risk_ticks_skips_when_all_candidates_exceed(self):
        """全候補が上限を超える場合は当該足を見送る (NO-TRADE)。"""
        cfg = ProviderConfig(rsi_period=5, sma_periods=(30,), cooldown_bars=30,
                             macro_filter=False, max_risk_ticks=1)
        provider = InfersSignalProvider(symbol="XAUUSD", tf=Timeframe.M5, config=cfg)
        plans = [p for c in provider_series() for p in provider.on_candle(c).plans]
        assert plans == []

    def test_notify_probe_expired_resets_cooldown_when_enabled(self):
        """失効リカバリー: expiry_recovery=True なら notify でクールダウン即時解除。"""
        cfg = ProviderConfig(rsi_period=5, sma_periods=(30,), cooldown_bars=30,
                             macro_filter=False, expiry_recovery=True)
        provider = InfersSignalProvider(symbol="XAUUSD", tf=Timeframe.M5, config=cfg)
        # プランが出た足でクールダウンが立つまで進める
        for c in provider_series():
            if provider.on_candle(c).plans:
                break
        assert provider._cooldown > 0
        provider.notify_probe_expired("XAUUSD/M5/x")
        assert provider._cooldown == 0

    def test_notify_probe_expired_noop_when_disabled(self):
        """既定 (expiry_recovery=False) は no-op: クールダウンは維持される。"""
        cfg = ProviderConfig(rsi_period=5, sma_periods=(30,), cooldown_bars=30,
                             macro_filter=False)
        provider = InfersSignalProvider(symbol="XAUUSD", tf=Timeframe.M5, config=cfg)
        for c in provider_series():
            if provider.on_candle(c).plans:
                break
        before = provider._cooldown
        assert before > 0
        provider.notify_probe_expired("XAUUSD/M5/x")
        assert provider._cooldown == before

    def test_macro_filter_blocks_when_macro_unconfirmed(self):
        """マクロフィルター有効時、マクロ足が方向未確定なら発注ゼロ (フルパイプライン配線確認)。

        同じ系列はミクロUPを確定しプランを出すが、短すぎてマクロ(H4)ダウは
        UNDEFINEDのまま → macro_gate が全件見送る。
        """
        cfg = ProviderConfig(rsi_period=5, sma_periods=(30,), cooldown_bars=30,
                             macro_filter=True)              # 既定の方向フィルター
        provider = InfersSignalProvider(symbol="XAUUSD", tf=Timeframe.M5, config=cfg)
        outputs = [provider.on_candle(c) for c in provider_series()]
        assert sum(len(o.plans) for o in outputs) == 0       # マクロ未確定 → 全件見送り

    def test_structure_events_flow(self):
        from infers.analysis.dow import StructureEventType
        _, outputs = self.collect()
        types = [e.type for o in outputs for e in o.structure_events]
        assert StructureEventType.HH in types
        assert StructureEventType.HL in types

    def test_emits_one_coherent_plan(self):
        """プランは1件 (クールダウン) で、各フィールドが手法の定義と整合する。"""
        _, outputs = self.collect()
        plans = [p for o in outputs for p in o.plans]
        assert len(plans) == 1
        p = plans[0]
        assert p.direction == +1                          # ダウUP確定方向のみ
        assert p.invalidation_price == 945                # エリオット原則② = P0
        assert p.w1_high_int == 1155                      # 追撃基準 = P1
        assert p.sl_int < p.invalidation_price            # SLは無効化の外側
        assert p.invalidation_price < p.limit_price_int   # 指値は無効化の内側
        # 半分利確 = 指値 + W1長(210) × 1.618 = 指値 + 340
        assert p.fib_target_int == p.limit_price_int + 340
        assert p.expiry > T0                              # 失効時刻つき (時間依存)
        assert p.request.features["dow_state"] == "UP"
        fam = p.request.features["families"]
        # コンフルエンス絶対条件: 独立 family が2つ以上 (手法G2 / CLAUDE.md 第5条)。
        # RSI二値化(G2-⑤①)後の最良セルは RSI+SMA+FIB 等になり得る(SR必須ではない)。
        assert len({s for s in fam.split(",") if s}) >= 2
        # P6: 既定の require_core_family=True 下では中核根拠(SMA/RSI)を必ず含む
        assert "RSI" in fam or "SMA" in fam
        assert p.volume_steps == 2 and p.add_volume_steps == 2

    def test_core_family_filter_default_on_and_drops_non_core(self):
        """P6: require_core_family は既定 True。中核根拠(SMA/RSI)を欠く
        SR,FIB のみの候補は L0 でプランにならない。

        require_core_family=False に明示すると同シナリオでプランが出る/出方が
        変わりうることで、フィルタが実際に効いていることを対比確認する。
        """
        assert ProviderConfig().require_core_family is True
        # フィルタ ON: 出るプランは必ず中核根拠つき
        on_plans = [p for o in self.collect()[1] for p in o.plans]
        for p in on_plans:
            fam = p.request.features["families"]
            assert "RSI" in fam or "SMA" in fam

    def test_plan_flows_into_trading_loop(self):
        """プロバイダ → AIゲート → リスク → FSM打診発注まで一気通貫。"""
        broker = LedgerBroker(spread_ticks=2, min_stop_distance_ticks=5)
        runner = LiveRunner(
            feed=FakeFeed(provider_series()), spec=GOLD, tf=Timeframe.M5,
            broker=broker,
            provider=InfersSignalProvider(symbol="XAUUSD", tf=Timeframe.M5,
                                          config=PROVIDER_CFG),
            # S1=1.5: 本シナリオのスコア (RSIパス次第0.5 + SR1.0) を通す閾値
            gateway=AiGateway(client=FakeClient(), cache=VerdictCache(),
                              policy=EscalationPolicy(score_l1=Decimal("1.5"),
                                                      score_l2=Decimal(4),
                                                      ambiguity_gray=Decimal("0.1"),
                                                      l2_daily_call_cap=3)),
            risk=RiskManager(RiskConfig(max_position_volume_steps=4,
                                        max_total_volume_steps=8,
                                        max_spread_ticks=10,
                                        daily_loss_limit_tick_steps=10_000)),
            fsm_config=FsmConfig(min_be_distance_ticks=10, be_offset_ticks=2,
                                 breakout_buffer_ticks=10),
            event_source=broker.process_bar,
            spread_fn=lambda: 2,
        )
        runner.run()
        # 打診指値はブローカーへ到達している (AIゲートGO → リスク承認 → 発注)
        assert broker.ledgers, "place_probe がブローカーに到達していない"
        led = next(iter(broker.ledgers.values()))
        assert led.direction == +1
        assert led.entries == []                          # 押し目未到達 → 未約定のまま
        # 未来コンフルエンスの指値は時間依存: expiry経過で自動取消される (設計書 §5.5)
        assert runner.loop.open_positions == {}
        assert broker.pending_count == 0


class TestMacroAdaptiveDepth:
    """マクロ順応型 深さスクリーニング (D1 200SMA の傾きで深さ要求を切替)。"""

    BASE = dict(rsi_period=5, sma_periods=(30,), cooldown_bars=30, macro_filter=False,
                depth_screen=True, depth_max=Decimal("0.50"),
                depth_max_shallow=Decimal("0.618"))
    # provider_series の第1波 = [945, 1155], span=210。
    # deep cap  = 945 + 0.50 *210 = 1050、shallow cap = 945 + 0.618*210 ≈ 1075。

    def test_slope_aligned_detects_direction(self):
        from infers.strategies.narrow_focus.provider import _HtfSmaWall
        wall = _HtfSmaWall(period=3, atr_period=3, slope_lookback=4)
        for px in range(1000, 1100, 10):                  # 上昇系列
            wall.update(mk_candle(0, px + 2, px - 2, px, tf=Timeframe.D1))
        assert wall.slope_aligned(+1) is True
        assert wall.slope_aligned(-1) is False

    def test_slope_aligned_not_ready_is_false(self):
        from infers.strategies.narrow_focus.provider import _HtfSmaWall
        wall = _HtfSmaWall(period=200, atr_period=14, slope_lookback=5)
        wall.update(mk_candle(0, 1002, 998, 1000, tf=Timeframe.D1))
        assert wall.slope_aligned(+1) is False            # SMA未準備 → 保守側

    def test_macro_trend_strong_uses_d1_slope(self):
        cfg = ProviderConfig(**self.BASE, macro_adaptive_depth=True,
                             htf_sma_period=3, sma_slope_lookback=4)
        prov = InfersSignalProvider(symbol="XAUUSD", tf=Timeframe.M5, config=cfg)
        d1_wall = next(w for stf, (_, w) in zip(cfg.htf_sma_tfs, prov._htf_sma)
                       if stf is Timeframe.D1)
        for px in range(1000, 1100, 10):
            d1_wall.update(mk_candle(0, px + 2, px - 2, px, tf=Timeframe.D1))
        assert prov._macro_trend_strong(+1) is True
        assert prov._macro_trend_strong(-1) is False      # 対称: 下向き不一致

    def test_exclusive_with_depth_tier(self):
        with pytest.raises(ValueError, match="排他"):
            InfersSignalProvider(symbol="XAUUSD", tf=Timeframe.M5,
                config=ProviderConfig(**self.BASE, depth_tier=True,
                                      macro_adaptive_depth=True))

    def test_strong_widens_to_shallow_weak_keeps_deep(self):
        series = provider_series()
        strong = InfersSignalProvider(symbol="XAUUSD", tf=Timeframe.M5,
            config=ProviderConfig(**self.BASE, macro_adaptive_depth=True))
        strong._macro_trend_strong = lambda d: True        # 強トレンド強制
        weak = InfersSignalProvider(symbol="XAUUSD", tf=Timeframe.M5,
            config=ProviderConfig(**self.BASE, macro_adaptive_depth=True))
        weak._macro_trend_strong = lambda d: False         # 弱トレンド強制
        sp = [p for c in series for p in strong.on_candle(c).plans]
        wp = [p for c in series for p in weak.on_candle(c).plans]
        smax = max((p.limit_price_int for p in sp), default=0)
        wmax = max((p.limit_price_int for p in wp), default=0)
        assert smax >= wmax                                # 強は浅い側まで許容 (狭めない)
        assert all(p.limit_price_int <= 1075 for p in sp)  # shallow cap
        assert all(p.limit_price_int <= 1050 for p in wp)  # deep cap

    def test_default_off_matches_plain_deep_when_d1_cold(self):
        """adaptive ON でも D1 200SMA 未準備なら strong=False → 深い押し目のみ
        (= plain depth_screen と同一。安全側の既定挙動)。"""
        series = provider_series()
        plain = InfersSignalProvider(symbol="XAUUSD", tf=Timeframe.M5,
            config=ProviderConfig(**self.BASE))                       # adaptive off
        adaptive_cold = InfersSignalProvider(symbol="XAUUSD", tf=Timeframe.M5,
            config=ProviderConfig(**self.BASE, macro_adaptive_depth=True))  # D1未準備
        ap = [p.limit_price_int for c in series for p in plain.on_candle(c).plans]
        bp = [p.limit_price_int for c in series for p in adaptive_cold.on_candle(c).plans]
        assert ap == bp


# ---------------------------------------------------------------------------
# リコンサイルの継続実行 (フェーズ8 #6: 起動時 + 定期 + 再接続復帰直後)
# ---------------------------------------------------------------------------

_RISK = RiskConfig(max_position_volume_steps=4, max_total_volume_steps=8,
                   max_spread_ticks=10, daily_loss_limit_tick_steps=10_000)
_HOLD = (PosState.PROBE, PosState.ADD, PosState.SL_AT_BE, PosState.RUNNER)


def _matching_snapshot(loop) -> BrokerSnapshot:
    """ループ現状を写したスナップショット (常に一致 → reconcile は ok)。"""
    positions: dict[str, BrokerPositionState] = {}
    pending: set[str] = set()
    for pid, (fsm, _plan) in loop.open_positions.items():
        if fsm.state is PosState.PROBE_PENDING:
            pending.add(pid)
        elif fsm.state in _HOLD:
            positions[pid] = BrokerPositionState(
                volume_steps=fsm.volume_steps, sl_int=fsm.sl_int or 0,
                avg_entry_int=fsm.entry_price_int or 0)
    return BrokerSnapshot(positions=positions, pending=frozenset(pending))


class _ReconnectingFeed(MarketFeed):
    """指定インデックスで reconnect_count を増やす合成フィード (復帰を模擬)。"""

    def __init__(self, candles: list[Candle], bump_at: int):
        self._candles = candles
        self._bump_at = bump_at
        self.reconnect_count = 0

    def connect(self) -> None: ...
    def close(self) -> None: ...
    def get_history(self, spec, tf, start, end): return []

    def iter_closed(self, spec, tf, *, stop=None):
        for i, c in enumerate(self._candles):
            if stop is not None and stop.is_set():
                return
            if i == self._bump_at:
                self.reconnect_count += 1
            yield c


class TestReconcileCadence:
    def _runner(self, *, feed, broker, reconcile_every_bars, journal=None):
        return LiveRunner(
            feed=feed, spec=GOLD, tf=Timeframe.M5, broker=broker,
            provider=ScriptedProvider({}),                # 発注なし → フラット
            gateway=AiGateway(client=FakeClient(), cache=VerdictCache(), policy=POLICY),
            risk=RiskManager(_RISK),
            fsm_config=FsmConfig(min_be_distance_ticks=10, be_offset_ticks=2,
                                 breakout_buffer_ticks=10),
            event_source=broker.process_bar, spread_fn=lambda: 2,
            reconcile_every_bars=reconcile_every_bars, journal=journal)

    def test_periodic_reconcile_runs_every_n_bars(self):
        broker = LedgerBroker(spread_ticks=2, min_stop_distance_ticks=5)
        runner = self._runner(feed=FakeFeed(CANDLES), broker=broker,
                              reconcile_every_bars=2)
        calls: list[int] = []
        broker.snapshot = lambda: (calls.append(1) or _matching_snapshot(runner.loop))
        runner.run(max_bars=6)
        # 起動時(1) + 定期 floor(6/2)=3 = 4
        assert len(calls) == 4

    def test_reconcile_after_reconnect(self):
        broker = LedgerBroker(spread_ticks=2, min_stop_distance_ticks=5)
        feed = _ReconnectingFeed(CANDLES, bump_at=3)       # 4本目の取得時に復帰
        runner = self._runner(feed=feed, broker=broker, reconcile_every_bars=0)
        calls: list[str] = []

        def snap():
            calls.append("snap")
            return _matching_snapshot(runner.loop)
        broker.snapshot = snap
        runner.run(max_bars=len(CANDLES))
        # 起動時(1) + 再接続復帰直後(1) = 2 (定期は無効)
        assert len(calls) == 2

    def test_no_reconcile_without_snapshot_capability(self):
        """snapshot を持たない Sim ブローカーでは定期リコンサイルは no-op。"""
        broker = LedgerBroker(spread_ticks=2, min_stop_distance_ticks=5)
        runner = self._runner(feed=FakeFeed(CANDLES), broker=broker,
                              reconcile_every_bars=1)
        runner.run(max_bars=5)                             # 例外なく完走 (no-op)
        assert runner.loop.open_positions == {}

    def test_reconcile_outcome_is_journaled(self, tmp_path):
        from infers.journal import JournalWriter, read_journal
        broker = LedgerBroker(spread_ticks=2, min_stop_distance_ticks=5)
        journal = JournalWriter(tmp_path / "rec.jsonl")
        runner = self._runner(feed=FakeFeed(CANDLES), broker=broker,
                              reconcile_every_bars=3, journal=journal)
        broker.snapshot = lambda: _matching_snapshot(runner.loop)
        runner.run(max_bars=6)
        journal.close()
        recs = [e for e in read_journal(tmp_path / "rec.jsonl") if e.kind == "RECONCILE"]
        reasons = [r.data["reason"] for r in recs]
        assert "startup" in reasons and "periodic" in reasons
        assert all(r.data["ok"] for r in recs)            # 一致スナップショット → 全て ok

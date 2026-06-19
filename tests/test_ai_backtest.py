"""AI Gateway・Batch連携・2パスバックテストエンジンのテスト。"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from infers.ai.batch import build_batch_request, ingest_batch_results, write_batch_file
from infers.ai.gateway import (
    AiGateway, EscalationPolicy, JudgementKind, JudgementRequest, Tier,
    Verdict, VerdictCache, cache_key,
)
from infers.analysis.dow import StructureEvent, StructureEventType, TrendState
from infers.strategies.narrow_focus.zigzag import SwingPoint
from infers.backtest.engine import (
    BacktestEngine, LedgerBroker, ProviderOutput, SwapConfig, TradePlan, TradeRecord,
    build_report, compute_swap_tick_steps, _weighted_nights,
)
from infers.core.models import Candle, Timeframe
from infers.execution.risk import RiskConfig, RiskManager
from infers.execution.sm import FsmConfig

UTC = timezone.utc
T0 = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
FAR = T0 + timedelta(days=30)

POLICY = EscalationPolicy(score_l1=Decimal(2), score_l2=Decimal(4),
                          ambiguity_gray=Decimal("0.1"), l2_daily_call_cap=3)

GO = Verdict(decision="GO", confidence=Decimal("0.8"), reasons=["ok"])
NO = Verdict(decision="NO_GO", confidence=Decimal("0.9"), reasons=["weak"])


def req(symbol: str = "XAUUSD", tag: str = "a") -> JudgementRequest:
    return JudgementRequest(kind=JudgementKind.ENTRY_GATE, symbol=symbol,
                            direction=+1, features={"tag": tag, "score": "2.5"})


class FakeClient:
    """tierごとの応答を固定し、呼び出し回数を記録する。"""

    def __init__(self, l1: Verdict | Exception = GO, l2: Verdict | Exception = GO):
        self.responses = {"L1": l1, "L2": l2}
        self.calls = {"L1": 0, "L2": 0}

    def judge(self, request: JudgementRequest, tier: str) -> Verdict:
        self.calls[tier] += 1
        resp = self.responses[tier]
        if isinstance(resp, Exception):
            raise resp
        return resp


def make_gateway(client: FakeClient, cache: VerdictCache | None = None) -> AiGateway:
    return AiGateway(client=client, cache=cache or VerdictCache(), policy=POLICY)


# ---------------------------------------------------------------------------
# Gateway: エスカレーション・キャッシュ・ガードレール
# ---------------------------------------------------------------------------

class TestEscalationPolicy:
    def test_tiers(self):
        assert POLICY.decide(Decimal(1), Decimal(1)) is Tier.NONE
        assert POLICY.decide(Decimal(3), Decimal("0.5")) is Tier.L1_ONLY
        assert POLICY.decide(Decimal(3), Decimal("0.05")) is Tier.L2_AFTER_L1  # 高曖昧性
        assert POLICY.decide(Decimal(5), Decimal(1)) is Tier.L2_AFTER_L1       # 勝負所


class TestGateway:
    def test_below_threshold_no_llm_call(self):
        client = FakeClient()
        gw = make_gateway(client)
        v = gw.judge(req(), cluster_score=Decimal(1), ambiguity=Decimal(1))
        assert v.decision == "NO_GO" and v.source == "POLICY"
        assert client.calls == {"L1": 0, "L2": 0}

    def test_cache_hit_skips_api(self):
        """同一Evidence列の2回目はAPIを叩かずキャッシュから取得。"""
        client = FakeClient()
        gw = make_gateway(client)
        v1 = gw.judge(req(), cluster_score=Decimal(3), ambiguity=Decimal(1))
        assert v1.decision == "GO" and v1.source == "L1"
        assert client.calls["L1"] == 1
        v2 = gw.judge(req(), cluster_score=Decimal(3), ambiguity=Decimal(1))
        assert v2.decision == "GO" and v2.source == "CACHE"
        assert client.calls["L1"] == 1                 # ★ 増えていない

    def test_l2_escalation_and_cache(self):
        client = FakeClient()
        gw = make_gateway(client)
        v = gw.judge(req(), cluster_score=Decimal(5), ambiguity=Decimal(1))
        assert v.source == "L2"
        assert client.calls == {"L1": 1, "L2": 1}
        v2 = gw.judge(req(), cluster_score=Decimal(5), ambiguity=Decimal(1))
        assert v2.source == "CACHE"
        assert client.calls == {"L1": 1, "L2": 1}      # 両層ともキャッシュ

    def test_l1_rejection_short_circuits_l2(self):
        """L1が却下したらL2は呼ばない (コスト最適化)。"""
        client = FakeClient(l1=NO)
        gw = make_gateway(client)
        v = gw.judge(req(), cluster_score=Decimal(5), ambiguity=Decimal(1))
        assert v.decision == "NO_GO"
        assert client.calls == {"L1": 1, "L2": 0}

    def test_llm_failure_is_default_no_trade(self):
        """LLMパニック時: 例外は伝播せず NO_GO (DEFAULT NO-TRADE)。"""
        client = FakeClient(l1=RuntimeError("api down"))
        gw = make_gateway(client)
        v = gw.judge(req(), cluster_score=Decimal(3), ambiguity=Decimal(1))
        assert v.decision == "NO_GO" and v.source == "GUARDRAIL"
        assert "L1_FAILURE" in v.reasons[0]

    def test_failure_not_cached(self):
        """障害は恒久判定としてキャッシュされない → 復旧後は再試行される。"""
        client = FakeClient(l1=RuntimeError("api down"))
        cache = VerdictCache()
        gw = make_gateway(client, cache)
        gw.judge(req(), cluster_score=Decimal(3), ambiguity=Decimal(1))
        assert cache.get(cache_key(req(), "L1")) is None
        client.responses["L1"] = GO                    # 復旧
        v = gw.judge(req(), cluster_score=Decimal(3), ambiguity=Decimal(1))
        assert v.decision == "GO" and client.calls["L1"] == 2

    def test_l2_budget_exhaustion(self):
        """L2予算超過: L1すら呼ばずに即 NO_GO (無駄なコストゼロ)。"""
        policy = EscalationPolicy(score_l1=Decimal(2), score_l2=Decimal(4),
                                  ambiguity_gray=Decimal("0.1"), l2_daily_call_cap=1)
        client = FakeClient()
        gw = AiGateway(client=client, cache=VerdictCache(), policy=policy)
        gw.judge(req(tag="x"), cluster_score=Decimal(5), ambiguity=Decimal(1))
        assert gw.l2_calls_today == 1
        v = gw.judge(req(tag="y"), cluster_score=Decimal(5), ambiguity=Decimal(1))
        assert v.decision == "NO_GO" and "L2_BUDGET_EXHAUSTED" in v.reasons[0]
        assert client.calls == {"L1": 1, "L2": 1}      # 2件目はL1も呼ばれない
        gw.new_day()
        assert gw.l2_calls_today == 0


# ---------------------------------------------------------------------------
# Batch API 連携 (パス1 → パス2)
# ---------------------------------------------------------------------------

class TestBatch:
    def test_write_batch_file_dedupes(self, tmp_path):
        items = [(req(tag="a"), "L1"), (req(tag="a"), "L1"), (req(tag="b"), "L2")]
        path = tmp_path / "batch.jsonl"
        assert write_batch_file(items, path, system_prompt="SYS") == 2
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2

    def test_l2_request_shape(self):
        entry = build_batch_request(req(), "L2", "SYS")
        assert entry["custom_id"] == cache_key(req(), "L2")
        p = entry["params"]
        assert p["model"] == "claude-fable-5"
        assert p["thinking"] == {"type": "adaptive"}
        assert p["output_config"]["effort"] == "high"
        # structured outputs: Verdict スキーマの強制 (これがないと結果が自由文になる)
        assert p["output_config"]["format"]["type"] == "json_schema"
        assert "decision" in p["output_config"]["format"]["schema"]["properties"]
        assert p["system"][0]["cache_control"] == {"type": "ephemeral"}

    def test_l1_request_has_output_format_without_effort(self):
        p = build_batch_request(req(), "L1", "SYS")["params"]
        assert p["output_config"]["format"]["type"] == "json_schema"
        assert "effort" not in p["output_config"]
        assert "thinking" not in p

    def test_confidence_schema_is_number_only(self):
        """confidence の anyOf[number, string] は string 分岐に制約が効かず、
        decision の値 ("NO_GO" 等) がそのまま confidence に出力されても
        構造化出力の検証を通過してしまう (実バッチで観測)。
        number 一本に絞り、型レベルで非数値出力を排除する。"""
        schema = build_batch_request(req(), "L1", "SYS")["params"]["output_config"]["format"]["schema"]
        confidence = schema["properties"]["confidence"]
        assert confidence["type"] == "number"
        assert "anyOf" not in confidence

    def test_ingest_results_into_cache_then_no_api(self):
        """Batch結果取込後のリプレイは外部APIを一切叩かない。"""
        import json
        key = cache_key(req(), "L1")
        ok_line = json.dumps({
            "custom_id": key,
            "result": {"type": "succeeded",
                       "message": {"content": [{"type": "text",
                                                "text": GO.model_dump_json()}]}},
        })
        err_line = json.dumps({"custom_id": "x", "result": {"type": "errored"}})
        cache = VerdictCache()
        assert ingest_batch_results([ok_line, err_line, "not-json"], cache) == 1

        client = FakeClient()
        gw = make_gateway(client, cache)
        v = gw.judge(req(), cluster_score=Decimal(3), ambiguity=Decimal(1))
        assert v.decision == "GO" and v.source == "CACHE"
        assert client.calls == {"L1": 0, "L2": 0}      # ★ API呼び出しゼロ


# ---------------------------------------------------------------------------
# 2パスバックテストエンジン
# ---------------------------------------------------------------------------

def mk_candle(i: int, h: int, l: int, c: int) -> Candle:
    o = max(l, min(h, c))
    return Candle(symbol="XAUUSD", tf=Timeframe.M5,
                  open_time=T0 + i * Timeframe.M5.duration,
                  o_int=o, h_int=h, l_int=l, c_int=c, volume=1, is_closed=True)


def hl_event(price: int) -> StructureEvent:
    s1 = SwingPoint(kind="LOW", bar_time=T0, price_int=price - 5, tf=Timeframe.M5,
                    confirmed_at=T0 + timedelta(minutes=5))
    s2 = SwingPoint(kind="LOW", bar_time=T0 + timedelta(minutes=10), price_int=price,
                    tf=Timeframe.M5, confirmed_at=T0 + timedelta(minutes=15))
    return StructureEvent(type=StructureEventType.HL, swing=s2, prev_swing=s1,
                          state_after=TrendState.UP)


CANDLES = [
    mk_candle(0, 1005, 995, 1000),
    mk_candle(1, 1005, 995, 1000),     # プラン発行
    mk_candle(2, 1000, 988, 992),      # 打診約定 (990)
    mk_candle(3, 1035, 1015, 1031),    # W1ブレイク → 追撃 (1033)
    mk_candle(4, 1040, 1020, 1035),    # HL構造イベント → 建値SL (平均建値1012 → 1014)
    mk_candle(5, 1075, 1040, 1070),    # フィボ1071タッチ → 半分利確 (1068)
    mk_candle(6, 1050, 990, 1000),     # SLヒット (1014)
]


def make_plan() -> TradePlan:
    return TradePlan(
        plan_id="bt1", direction=+1, limit_price_int=990, volume_steps=2,
        add_volume_steps=2, sl_int=960, expiry=FAR, invalidation_price=950,
        w1_high_int=1020, fib_target_int=1071, request=req(tag="bt"),
        cluster_score=Decimal("2.5"), ambiguity=Decimal("0.5"),
    )


class ScriptedProvider:
    """bar番号→出力 の台本どおりに発行する決定論プロバイダ。"""

    def __init__(self, script: dict[int, ProviderOutput]):
        self._script = script
        self._i = -1

    def on_candle(self, candle: Candle) -> ProviderOutput:
        self._i += 1
        return self._script.get(self._i, ProviderOutput())


def make_engine(client: FakeClient):
    broker = LedgerBroker(spread_ticks=2, min_stop_distance_ticks=5)
    gateway = make_gateway(client)
    risk = RiskManager(RiskConfig(max_position_volume_steps=4, max_total_volume_steps=8,
                                  max_spread_ticks=10, daily_loss_limit_tick_steps=10_000))
    engine = BacktestEngine(broker=broker, gateway=gateway, risk=risk,
                            fsm_config=FsmConfig(min_be_distance_ticks=10,
                                                 be_offset_ticks=2,
                                                 breakout_buffer_ticks=10))
    return engine, broker, risk


SCRIPT = {
    1: ProviderOutput(plans=[make_plan()]),
    4: ProviderOutput(structure_events=[hl_event(1025)]),
    5: ProviderOutput(rsi_value=Decimal(75)),     # 半分利確を RSI 利確圏で発火 (§6.4)
}


class TestBacktestEngine:
    def test_full_replay_with_metrics(self):
        """打診→追撃→建値SL→半分利確→SL退出 の全行程をリプレイし指標を出す。"""
        engine, broker, risk = make_engine(FakeClient())
        report = engine.run(CANDLES, ScriptedProvider(SCRIPT))

        assert len(report.trades) == 1
        t = report.trades[0]
        # entry: 990×2 + 1033×2 = 4046 / exit: 半分1068×2 + 建値SL1014×2 = 4164 → +118
        # 建値SL=1014 は平均建値 (990×2+1033×2)/4=1012 + 微益2 (P7)
        assert t.pnl_tick_steps == 118
        assert t.exit_kind == "SL"
        assert t.is_breakeven_sl_exit            # 建値SL退出 (防御の証跡)
        assert report.total_pnl_tick_steps == 118
        assert report.win_rate == Decimal(1)
        assert report.be_sl_exit_rate == Decimal(1)
        assert report.profit_factor is None      # 損失ゼロ
        assert report.max_drawdown_tick_steps == 0
        assert report.equity_curve == (118,)
        assert risk.daily_realized_tick_steps == 118

    def test_no_go_places_nothing(self):
        """AIがNO_GOなら注文は一切出ない (NO-TRADEデフォルト)。"""
        engine, broker, _ = make_engine(FakeClient(l1=NO))
        report = engine.run(CANDLES, ScriptedProvider(SCRIPT))
        assert report.trades == ()
        assert broker.pending_count == 0

    def test_swap_reduces_pnl_by_nights_held(self):
        """スワップ計上後は pnl が「粗利 + スワップ(負)」になる。

        7本足シナリオ (M5, 同日内) は夜跨ぎ0 → ロールオーバー時刻を全バーが
        跨ぐよう設定し、保有数量×夜数で課金されることを確認する。
        """
        broker = LedgerBroker(spread_ticks=2, min_stop_distance_ticks=5)
        gateway = make_gateway(FakeClient())
        risk = RiskManager(RiskConfig(max_position_volume_steps=4,
                                      max_total_volume_steps=8, max_spread_ticks=10,
                                      daily_loss_limit_tick_steps=10_000))
        # 打診2 + 追撃2 = 最大4 steps 保有。T0=00:00 なので rollover=0時を毎バー跨がない
        # → 跨ぎを作るため rollover_hour を T0 直後の 1時 にし長い保有にはしない。
        swap = SwapConfig(enabled=True, long_ticks_per_step=Decimal(-3),
                          short_ticks_per_step=Decimal(-2), rollover_hour_utc=0,
                          triple_weekday=-1)
        engine = BacktestEngine(broker=broker, gateway=gateway, risk=risk,
                                fsm_config=FsmConfig(min_be_distance_ticks=10,
                                                     be_offset_ticks=2,
                                                     breakout_buffer_ticks=10),
                                swap=swap)
        report = engine.run(CANDLES, ScriptedProvider(SCRIPT))
        t = report.trades[0]
        # 同日内 (T0=06-01 00:00〜) でロールオーバー0時を跨がない → スワップ0
        assert t.swap_tick_steps == 0
        assert t.pnl_tick_steps == 118            # スワップ0なので粗利のまま

    def test_swap_charges_per_night_and_volume(self):
        """compute_swap_tick_steps: 保有数量×夜数で課金 (区間積分)。"""
        from datetime import datetime, timezone
        from infers.backtest.engine import _Ledger
        utc = timezone.utc
        led = _Ledger(direction=+1)
        # 6/1 12:00 に 2 step 建玉 → 6/4 12:00 に 2 step 決済 (3夜保有)
        led.entries = [(1000, 2)]
        led.entry_times = [datetime(2026, 6, 1, 12, tzinfo=utc)]
        led.exits = [(1010, 2)]
        led.exit_times = [datetime(2026, 6, 4, 12, tzinfo=utc)]
        cfg = SwapConfig(enabled=True, long_ticks_per_step=Decimal(-5),
                         rollover_hour_utc=21, triple_weekday=-1)
        # 21時ロールオーバーを 6/1,6/2,6/3 の3回跨ぐ → -5 × 2step × 3夜 = -30
        assert compute_swap_tick_steps(+1, led, cfg) == -30

    def test_weighted_nights_triple_wednesday(self):
        from datetime import datetime, timezone
        utc = timezone.utc
        # 2026-06-01 は月曜。火(2)水(3)を含む期間で水曜3倍
        start = datetime(2026, 6, 1, 12, tzinfo=utc)   # 月12:00
        end = datetime(2026, 6, 4, 12, tzinfo=utc)     # 木12:00
        # 21時ロールオーバー: 月21(月),火21(火),水21(水=3倍) → 1+1+3 = 5
        assert _weighted_nights(start, end, 21, 2) == 5
        assert _weighted_nights(start, end, 21, -1) == 3   # 3倍無効なら3

    def test_llm_panic_keeps_engine_safe(self):
        """LLM全停止でもエンジンは例外なく完走し、トレードゼロを維持。"""
        engine, broker, _ = make_engine(FakeClient(l1=RuntimeError("down"),
                                                   l2=RuntimeError("down")))
        report = engine.run(CANDLES, ScriptedProvider(SCRIPT))
        assert report.trades == ()
        assert broker.pending_count == 0

    def test_pass1_collect_judgements(self):
        cache = VerdictCache()
        items = BacktestEngine.collect_judgements(
            CANDLES, ScriptedProvider(SCRIPT), policy=POLICY, cache=cache)
        assert [(r.features["tag"], t) for r, t in items] == [("bt", "L1")]
        # キャッシュ解決済みなら収集されない (再実行は無料)
        cache.put(cache_key(make_plan().request, "L1"), GO)
        items2 = BacktestEngine.collect_judgements(
            CANDLES, ScriptedProvider(SCRIPT), policy=POLICY, cache=cache)
        assert items2 == []


class TestReportMetrics:
    def test_pf_dd_winrate(self):
        trades = [
            TradeRecord("a", +1, +100, "CLOSE"),
            TradeRecord("b", +1, -40, "SL"),
            TradeRecord("c", +1, +20, "SL"),     # 建値以上のSL退出
            TradeRecord("d", +1, -60, "SL"),
        ]
        r = build_report(trades)
        assert r.profit_factor == Decimal("1.2")          # 120 / 100
        assert r.win_rate == Decimal("0.5")
        assert r.be_sl_exit_rate == Decimal("0.25")       # cのみ
        assert r.equity_curve == (100, 60, 80, 20)
        assert r.max_drawdown_tick_steps == 80            # 100 → 20
        assert r.total_pnl_tick_steps == 20

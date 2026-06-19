"""フェーズ10 修正の回帰テスト。

対象:
  1. TradingLoop の日次境界で new_day() が決定論的に呼ばれる
  2. 戦略プロバイダの FIBリトレースメント水準 (FIBファミリー配線)
  3. AiGateway の判定集計 (ガードレールNO_GOの可視化)
  4. backtest/replay 用 CacheOnlyClient のフェイルセーフ
  5. python -m infers.backtest のサブコマンドマッピング
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from infers.ai.gateway import (
    AiGateway, EscalationPolicy, JudgementKind, JudgementRequest, Verdict,
    VerdictCache, cache_key,
)
from infers.core.loop import ProviderOutput, TradingLoop
from infers.core.models import Candle, Timeframe
from infers.execution.sim_broker import SimBroker
from infers.execution.sm import FsmConfig
from infers.main import CacheOnlyClient
from infers.analysis.dow import TrendState
from infers.strategies.narrow_focus.provider import (
    FIB_RETRACE_RATIOS, MacroResampler, fib_retrace_levels, macro_gate,
)

UTC = timezone.utc

POLICY = EscalationPolicy(score_l1=Decimal(2), score_l2=Decimal(4),
                          ambiguity_gray=Decimal("0.1"), l2_daily_call_cap=3)
FSM_CFG = FsmConfig(min_be_distance_ticks=10, be_offset_ticks=2,
                    breakout_buffer_ticks=10)


def _candle(open_time: datetime) -> Candle:
    return Candle(symbol="XAUUSD", tf=Timeframe.M5, open_time=open_time,
                  o_int=100, h_int=110, l_int=90, c_int=105,
                  volume=1, is_closed=True)


def _request() -> JudgementRequest:
    return JudgementRequest(kind=JudgementKind.FUTURE_CONFLUENCE_REVIEW,
                            symbol="XAUUSD", direction=1, features={"x": "1"})


# ---------------------------------------------------------------------------
# 1. 日次境界
# ---------------------------------------------------------------------------

class _Recorder:
    """gateway/risk の new_day 呼び出し記録 (ダックタイピング)。"""

    def __init__(self) -> None:
        self.new_days = 0

    def new_day(self) -> None:
        self.new_days += 1


class TestDayBoundary:
    def _loop(self, gateway, risk) -> TradingLoop:
        broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        return TradingLoop(broker=broker, gateway=gateway, risk=risk,
                           fsm_config=FSM_CFG)

    def test_new_day_called_on_utc_date_change_only(self):
        gw, rk = _Recorder(), _Recorder()
        loop = self._loop(gw, rk)
        empty = ProviderOutput()

        loop.on_candle(_candle(datetime(2026, 1, 1, 23, 50, tzinfo=UTC)),
                       empty, spread_ticks=2)
        loop.on_candle(_candle(datetime(2026, 1, 1, 23, 55, tzinfo=UTC)),
                       empty, spread_ticks=2)
        assert (gw.new_days, rk.new_days) == (0, 0)  # 初日はリセットしない

        loop.on_candle(_candle(datetime(2026, 1, 2, 0, 0, tzinfo=UTC)),
                       empty, spread_ticks=2)
        assert (gw.new_days, rk.new_days) == (1, 1)

        loop.on_candle(_candle(datetime(2026, 1, 2, 0, 5, tzinfo=UTC)),
                       empty, spread_ticks=2)
        assert (gw.new_days, rk.new_days) == (1, 1)  # 同日内は再リセットしない

    def test_weekend_gap_counts_as_one_boundary(self):
        gw, rk = _Recorder(), _Recorder()
        loop = self._loop(gw, rk)
        empty = ProviderOutput()
        loop.on_candle(_candle(datetime(2026, 1, 2, 21, 55, tzinfo=UTC)),
                       empty, spread_ticks=2)  # 金曜
        loop.on_candle(_candle(datetime(2026, 1, 5, 0, 0, tzinfo=UTC)),
                       empty, spread_ticks=2)  # 月曜
        assert (gw.new_days, rk.new_days) == (1, 1)


class TestExpiryRecoverySink:
    """失効リカバリー: loop が「時間切れ失効」のみ expiry_sink を呼ぶ。"""

    def _probe_loop(self, sink):
        from infers.execution.sm import PositionFSM, PosState
        from infers.core.loop import TradePlan
        broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        broker.process_bar(_candle(t0))
        loop = TradingLoop(broker=broker, gateway=_Recorder(), risk=_Recorder(),
                           fsm_config=FSM_CFG, expiry_sink=sink)
        fsm = PositionFSM(position_id="pid", direction=+1, broker=broker, config=FSM_CFG)
        # limit 90 / sl 60 / invalidation 50。終値 105 (>50) なので無効化はしない。
        fsm.place_probe(limit_price_int=90, volume_steps=2, sl_int=60,
                        expiry=t0 + 2 * Timeframe.M5.duration, invalidation_price=50)
        assert fsm.state is PosState.PROBE_PENDING
        loop.open_positions["pid"] = (fsm, object())
        return loop, t0

    def test_expired_invokes_sink(self):
        called: list[str] = []
        loop, t0 = self._probe_loop(called.append)
        # expiry 経過した足 (close_time >= expiry) で失効 → sink 発火
        loop.on_candle(_candle(t0 + 3 * Timeframe.M5.duration),
                       ProviderOutput(), spread_ticks=2)
        assert called == ["pid"]

    def test_invalidated_does_not_invoke_sink(self):
        called: list[str] = []
        broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
        t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        broker.process_bar(_candle(t0))
        loop = TradingLoop(broker=broker, gateway=_Recorder(), risk=_Recorder(),
                           fsm_config=FSM_CFG, expiry_sink=called.append)
        from infers.execution.sm import PositionFSM
        fsm = PositionFSM(position_id="pid", direction=+1, broker=broker, config=FSM_CFG)
        # 無効化 50、終値が 40 で割り込む。expiry は遠い未来。
        fsm.place_probe(limit_price_int=90, volume_steps=2, sl_int=45,
                        expiry=t0 + 9999 * Timeframe.M5.duration, invalidation_price=50)
        loop.open_positions["pid"] = (fsm, object())
        bar = Candle(symbol="XAUUSD", tf=Timeframe.M5,
                     open_time=t0 + 1 * Timeframe.M5.duration,
                     o_int=60, h_int=60, l_int=40, c_int=40, volume=1, is_closed=True)
        loop.on_candle(bar, ProviderOutput(), spread_ticks=2)
        assert called == []          # シナリオ崩壊はリカバリー対象外


# ---------------------------------------------------------------------------
# 2. FIBリトレースメント水準
# ---------------------------------------------------------------------------

class TestFibRetraceLevels:
    def test_up_wave_levels_descend_from_p1(self):
        # P0=1000 → P1=2000 (上昇第1波)。押し目は P1 から下へ
        assert fib_retrace_levels(1000, 2000) == [1618, 1500, 1382, 1214]

    def test_down_wave_levels_ascend_from_p1(self):
        assert fib_retrace_levels(2000, 1000) == [1382, 1500, 1618, 1786]

    def test_levels_stay_inside_wave1_range(self):
        for p0, p1 in ((1000, 2000), (2000, 1000)):
            lo, hi = min(p0, p1), max(p0, p1)
            for lvl in fib_retrace_levels(p0, p1, FIB_RETRACE_RATIOS):
                assert lo < lvl < hi


# ---------------------------------------------------------------------------
# 2b. マクロ方向フィルター (設計書 §1 フラクタル: 上位足トレンドと一致時のみ発注)
# ---------------------------------------------------------------------------

class TestMacroGate:
    def test_macro_up_allows_only_buys(self):
        assert macro_gate(+1, TrendState.UP) == +1     # 一致 → 通す
        assert macro_gate(-1, TrendState.UP) is None    # 逆行 → 見送り

    def test_macro_down_allows_only_sells(self):
        assert macro_gate(-1, TrendState.DOWN) == -1
        assert macro_gate(+1, TrendState.DOWN) is None

    def test_macro_unconfirmed_blocks_all(self):
        # マクロ未確定/警戒なら NO-TRADE (両方向見送り)
        for state in (TrendState.UNDEFINED, TrendState.UP_SUSPECT,
                      TrendState.DOWN_SUSPECT):
            assert macro_gate(+1, state) is None
            assert macro_gate(-1, state) is None

    def test_micro_none_stays_none(self):
        assert macro_gate(None, TrendState.UP) is None


class TestMacroResampler:
    def _m5(self, i: int, o, h, l, c) -> Candle:
        # i は M5 本数。2026-06-01 00:00 UTC 起点
        t = datetime(2026, 6, 1, tzinfo=UTC) + i * Timeframe.M5.duration
        return Candle(symbol="XAUUSD", tf=Timeframe.M5, open_time=t,
                      o_int=o, h_int=h, l_int=l, c_int=c, volume=1, is_closed=True)

    def test_h1_bucket_aggregates_ohlc(self):
        rs = MacroResampler("XAUUSD", Timeframe.H1)
        # H1 = M5 ×12。最初の12本は同一バケット → 確定足は出ない
        for i in range(12):
            assert rs.push(self._m5(i, 100 + i, 110 + i, 90 + i, 105 + i)) is None
        # 13本目 (次のH1) 投入で直前H1が確定
        bar = rs.push(self._m5(12, 200, 210, 190, 205))
        assert bar is not None and bar.tf is Timeframe.H1
        assert bar.o_int == 100                       # 先頭の始値
        assert bar.h_int == 110 + 11                  # 期間中の最高値
        assert bar.l_int == 90                         # 期間中の最安値
        assert bar.c_int == 105 + 11                  # 末尾の終値
        assert bar.open_time == datetime(2026, 6, 1, tzinfo=UTC)

    def test_boundary_aligned_to_clock(self):
        rs = MacroResampler("XAUUSD", Timeframe.H4)
        # 03:55 と 04:00 は別のH4バケット (0-4時 と 4-8時)
        b1 = datetime(2026, 6, 1, 3, 55, tzinfo=UTC)
        rs.push(Candle(symbol="XAUUSD", tf=Timeframe.M5, open_time=b1,
                       o_int=100, h_int=110, l_int=90, c_int=105, volume=1,
                       is_closed=True))
        nxt = rs.push(Candle(symbol="XAUUSD", tf=Timeframe.M5,
                             open_time=datetime(2026, 6, 1, 4, 0, tzinfo=UTC),
                             o_int=105, h_int=115, l_int=95, c_int=108, volume=1,
                             is_closed=True))
        assert nxt is not None                         # 04:00で前バケット確定
        assert nxt.open_time == datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 3. 判定集計 (ガードレール可視化)
# ---------------------------------------------------------------------------

class _BoomClient:
    def judge(self, request, tier):
        raise RuntimeError("simulated failure")


class _GoClient:
    def judge(self, request, tier):
        return Verdict(decision="GO", confidence=Decimal(1), source=tier)


class TestGatewayStats:
    def test_guardrail_no_go_is_counted_with_reason(self):
        gw = AiGateway(client=_BoomClient(), cache=VerdictCache(), policy=POLICY)
        v = gw.judge(_request(), cluster_score=Decimal(2), ambiguity=Decimal("0.5"))
        assert (v.decision, v.source) == ("NO_GO", "GUARDRAIL")
        assert gw.stats["GUARDRAIL:NO_GO"] == 1
        assert gw.guardrail_reasons["L1_FAILURE: RuntimeError"] == 1

    def test_policy_and_cache_paths_are_counted(self):
        cache = VerdictCache()
        gw = AiGateway(client=_GoClient(), cache=cache, policy=POLICY)
        req = _request()

        gw.judge(req, cluster_score=Decimal(1), ambiguity=Decimal("0.5"))
        assert gw.stats["POLICY:NO_GO"] == 1

        gw.judge(req, cluster_score=Decimal(2), ambiguity=Decimal("0.5"))
        assert gw.stats["L1:GO"] == 1

        gw.judge(req, cluster_score=Decimal(2), ambiguity=Decimal("0.5"))
        assert gw.stats["CACHE:GO"] == 1
        assert not gw.guardrail_reasons


# ---------------------------------------------------------------------------
# 4. CacheOnlyClient (replay のフェイルセーフ)
# ---------------------------------------------------------------------------

class TestCacheOnlyClient:
    def test_cache_miss_becomes_guardrail_no_go(self):
        gw = AiGateway(client=CacheOnlyClient(), cache=VerdictCache(), policy=POLICY)
        v = gw.judge(_request(), cluster_score=Decimal(2), ambiguity=Decimal("0.5"))
        assert (v.decision, v.source) == ("NO_GO", "GUARDRAIL")
        assert gw.guardrail_reasons["L1_FAILURE: CacheMissError"] == 1

    def test_cached_verdict_is_served_without_llm(self):
        cache = VerdictCache()
        req = _request()
        cache.put(cache_key(req, "L1"),
                  Verdict(decision="GO", confidence=Decimal(1), source="L1"))
        gw = AiGateway(client=CacheOnlyClient(), cache=cache, policy=POLICY)
        v = gw.judge(req, cluster_score=Decimal(2), ambiguity=Decimal("0.5"))
        assert (v.decision, v.source) == ("GO", "CACHE")


# ---------------------------------------------------------------------------
# 4a. 2段階バッチ収集 (L2=Fable 5 のコスト制御)
# ---------------------------------------------------------------------------

class _PlanProvider:
    """指定した (score, ambiguity, features) のプランを順番に1つずつ発行する。"""

    def __init__(self, specs: list[tuple[Decimal, Decimal, str]]) -> None:
        self._specs = list(specs)

    def on_candle(self, candle):
        from infers.core.loop import ProviderOutput, TradePlan
        out = ProviderOutput()
        if not self._specs:
            return out
        score, amb, tag = self._specs.pop(0)
        req = JudgementRequest(kind=JudgementKind.FUTURE_CONFLUENCE_REVIEW,
                               symbol="XAUUSD", direction=1, features={"tag": tag})
        out.plans.append(TradePlan(
            plan_id=tag, direction=1, limit_price_int=100, volume_steps=2,
            add_volume_steps=2, sl_int=90, expiry=candle.open_time,
            invalidation_price=95, w1_high_int=110, fib_target_int=120,
            request=req, cluster_score=score, ambiguity=amb))
        return out


class TestTwoStageCollect:
    L2_SPEC = (Decimal(5), Decimal(1))      # score>=score_l2 → L2_AFTER_L1
    L1_SPEC = (Decimal(2), Decimal("0.5"))  # L1_ONLY

    def _candles(self, n: int, start_day: int = 1):
        return [_candle(datetime(2026, 1, start_day, 1, 5 * i % 60, tzinfo=UTC))
                for i in range(n)]

    def _collect(self, specs, cache, tier, candles=None):
        from infers.backtest.engine import BacktestEngine
        return BacktestEngine.collect_judgements(
            candles or self._candles(len(specs)), _PlanProvider(specs),
            policy=POLICY, cache=cache, tier=tier)

    def test_l1_stage_collects_no_l2(self):
        specs = [(*self.L2_SPEC, "a"), (*self.L1_SPEC, "b")]
        items = self._collect(specs, VerdictCache(), "L1")
        assert [t for _, t in items] == ["L1", "L1"]

    def test_l2_stage_requires_l1_go_in_cache(self):
        cache = VerdictCache()
        specs = [(*self.L2_SPEC, "go"), (*self.L2_SPEC, "nogo"),
                 (*self.L2_SPEC, "unresolved")]
        reqs = {tag: JudgementRequest(kind=JudgementKind.FUTURE_CONFLUENCE_REVIEW,
                                      symbol="XAUUSD", direction=1,
                                      features={"tag": tag})
                for _, _, tag in specs}
        cache.put(cache_key(reqs["go"], "L1"),
                  Verdict(decision="GO", confidence=Decimal(1), source="L1"))
        cache.put(cache_key(reqs["nogo"], "L1"),
                  Verdict(decision="NO_GO", confidence=Decimal(1), source="L1"))

        items = self._collect(specs, cache, "L2")
        assert [(r.features["tag"], t) for r, t in items] == [("go", "L2")]

    def test_l2_stage_respects_daily_budget(self):
        cache = VerdictCache()
        n = POLICY.l2_daily_call_cap + 2
        specs = [(*self.L2_SPEC, f"p{i}") for i in range(n)]
        for _, _, tag in specs:
            req = JudgementRequest(kind=JudgementKind.FUTURE_CONFLUENCE_REVIEW,
                                   symbol="XAUUSD", direction=1,
                                   features={"tag": tag})
            cache.put(cache_key(req, "L1"),
                      Verdict(decision="GO", confidence=Decimal(1), source="L1"))

        same_day = self._collect(specs, cache, "L2")
        assert len(same_day) == POLICY.l2_daily_call_cap  # 同一日: 予算で打ち切り

        # 日付が変われば予算はリセットされる
        specs2 = [(*self.L2_SPEC, f"p{i}") for i in range(n)]
        candles = (self._candles(POLICY.l2_daily_call_cap, start_day=1)
                   + self._candles(n - POLICY.l2_daily_call_cap, start_day=2))
        two_days = self._collect(specs2, cache, "L2", candles=candles)
        assert len(two_days) == n

    def test_all_tier_keeps_legacy_behaviour(self):
        specs = [(*self.L2_SPEC, "a"), (*self.L1_SPEC, "b")]
        items = self._collect(specs, VerdictCache(), "ALL")
        assert sorted(t for _, t in items) == ["L1", "L1", "L2"]


# ---------------------------------------------------------------------------
# 4b. Batch結果取込の可視化と救済パース
# ---------------------------------------------------------------------------

class TestBatchIngestStats:
    def _line(self, key: str, text: str) -> str:
        import json
        return json.dumps({
            "custom_id": key,
            "result": {"type": "succeeded",
                       "message": {"content": [{"type": "text", "text": text}]}},
        })

    def test_stats_breakdown_explains_zero_ingest(self):
        import json
        from collections import Counter

        from infers.ai.batch import ingest_batch_results
        lines = [
            self._line("k1", "I think the answer is GO, confidence high."),
            json.dumps({"custom_id": "k2", "result": {"type": "errored"}}),
            "broken{",
        ]
        stats: Counter = Counter()
        cache = VerdictCache()
        assert ingest_batch_results(lines, cache, stats=stats) == 0
        assert stats["parse_failed"] == 1      # 自由文 (旧バッチの症状)
        assert stats["errored"] == 1
        assert stats["malformed_line"] == 1
        assert stats["ingested"] == 0

    def test_lenient_parse_salvages_json_embedded_in_prose(self):
        from infers.ai.batch import ingest_batch_results
        verdict_json = Verdict(decision="GO", confidence=Decimal("0.8"),
                               source="LLM").model_dump_json()
        text = f"Here is my judgement:\n```json\n{verdict_json}\n```\nThanks."
        cache = VerdictCache()
        assert ingest_batch_results([self._line("k1", text)], cache) == 1
        assert cache.get("k1").decision == "GO"

    def test_excess_reasons_are_clamped_not_dropped(self):
        # structured outputs は maxItems を強制しない — reasons が4件以上でも
        # 先頭3件へ切り詰めて取り込む (decision/confidence は不変)
        import json
        from infers.ai.batch import ingest_batch_results
        text = json.dumps({
            "decision": "WAIT", "confidence": "0.58",
            "reasons": ["r1", "r2", "r3", "r4"],
            "selected_wave_count": None, "source": "LLM",
        })
        cache = VerdictCache()
        assert ingest_batch_results([self._line("k1", text)], cache) == 1
        verdict = cache.get("k1")
        assert verdict.decision == "WAIT"
        assert verdict.reasons == ("r1", "r2", "r3") or \
            list(verdict.reasons) == ["r1", "r2", "r3"]


# ---------------------------------------------------------------------------
# 5. python -m infers.backtest のサブコマンドマッピング
# ---------------------------------------------------------------------------

class TestBacktestCliWrapper:
    def test_subcommands_map_to_modes(self):
        from infers.backtest.__main__ import _argv
        assert _argv(["run", "--data", "d.parquet"]) == \
            ["--mode", "backtest", "--data", "d.parquet"]
        assert _argv(["judge", "--batch"]) == ["--mode", "judge", "--batch"]
        assert _argv(["replay"]) == ["--mode", "replay"]

    def test_unknown_subcommand_exits(self):
        from infers.backtest.__main__ import _argv
        with pytest.raises(SystemExit):
            _argv(["bogus"])

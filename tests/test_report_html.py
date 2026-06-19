"""HTMLレポート (backtest/report_html.py) のテスト。

test_ai_backtest.py と同一の7本足シナリオ (打診→追撃→建値SL→半分利確→SL退出)
を recorder つきで走らせ、収集されるトレード詳細・USD換算・出力ファイルを検証する。
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from infers.ai.gateway import (
    AiGateway, EscalationPolicy, JudgementKind, JudgementRequest, Verdict, VerdictCache,
)
from infers.analysis.dow import StructureEvent, StructureEventType, TrendState
from infers.strategies.narrow_focus.zigzag import SwingPoint
from infers.backtest.engine import BacktestEngine, LedgerBroker, ProviderOutput, TradePlan
from infers.backtest.report_html import (
    BacktestRecorder, RecordingGateway, build_report_data, classify_exits,
    write_html_report,
)
from infers.core.models import Candle, SymbolSpec, Timeframe
from infers.execution.risk import RiskConfig, RiskManager
from infers.execution.sm import FsmConfig

UTC = timezone.utc
T0 = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
FAR = T0 + timedelta(days=30)
BAR = Timeframe.M5.duration

SPEC = SymbolSpec(name="XAUUSD", tick_size=Decimal("0.01"),
                  lot_step=Decimal("0.01"), digits=2)

GO = Verdict(decision="GO", confidence=Decimal("0.8"), reasons=["ok"])


class FakeClient:
    def judge(self, request, tier):
        return GO


def req(tag: str = "bt") -> JudgementRequest:
    return JudgementRequest(kind=JudgementKind.ENTRY_GATE, symbol="XAUUSD",
                            direction=+1, features={"tag": tag, "score": "2.5"})


def mk_candle(i: int, h: int, l: int, c: int) -> Candle:
    o = max(l, min(h, c))
    return Candle(symbol="XAUUSD", tf=Timeframe.M5, open_time=T0 + i * BAR,
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
    mk_candle(4, 1040, 1020, 1035),    # HL → 建値SL (平均建値1012+2=1014)
    mk_candle(5, 1075, 1040, 1070),    # フィボ1071タッチ → 半分利確 (1068)
    mk_candle(6, 1050, 990, 1000),     # SLヒット (1014)
]


def make_plan(**overrides) -> TradePlan:
    kw = dict(
        plan_id="bt1", direction=+1, limit_price_int=990, volume_steps=2,
        add_volume_steps=2, sl_int=960, expiry=FAR, invalidation_price=950,
        w1_high_int=1020, fib_target_int=1071, request=req(),
        cluster_score=Decimal("2.5"), ambiguity=Decimal("0.5"),
    )
    kw.update(overrides)
    return TradePlan(**kw)


class ScriptedProvider:
    def __init__(self, script: dict[int, ProviderOutput]):
        self._script = script
        self._i = -1

    def on_candle(self, candle: Candle) -> ProviderOutput:
        self._i += 1
        return self._script.get(self._i, ProviderOutput())


def run_with_recorder(script: dict[int, ProviderOutput]):
    gateway = RecordingGateway(AiGateway(
        client=FakeClient(), cache=VerdictCache(),
        policy=EscalationPolicy(score_l1=Decimal(2), score_l2=Decimal(4),
                                ambiguity_gray=Decimal("0.1"), l2_daily_call_cap=99)))
    recorder = BacktestRecorder(gateway=gateway)
    engine = BacktestEngine(
        broker=LedgerBroker(spread_ticks=2, min_stop_distance_ticks=5),
        gateway=gateway,
        risk=RiskManager(RiskConfig(max_position_volume_steps=4,
                                    max_total_volume_steps=8, max_spread_ticks=10,
                                    daily_loss_limit_tick_steps=10_000)),
        fsm_config=FsmConfig(min_be_distance_ticks=10, be_offset_ticks=2,
                             breakout_buffer_ticks=10))
    report = engine.run(CANDLES, ScriptedProvider(script), recorder=recorder)
    return report, recorder


SCRIPT = {1: ProviderOutput(plans=[make_plan()]),
          4: ProviderOutput(structure_events=[hl_event(1025)]),
          5: ProviderOutput(rsi_value=Decimal(75))}     # 半分利確を RSI 利確圏で発火


def unix(i: int) -> int:
    """bar i の close_time (unix秒)。"""
    return int((T0 + i * BAR + BAR).timestamp())


class TestRecorder:
    def test_trade_detail_collected_with_times(self):
        report, rec = run_with_recorder(SCRIPT)
        assert len(report.trades) == 1 and len(rec.trades) == 1
        t = rec.trades[0]
        # 約定列 (時刻はそれぞれの bar の close_time)
        assert t["entries"] == [[unix(2), 990, 2], [unix(3), 1033, 2]]
        assert t["exits"] == [[unix(5), 1068, 2], [unix(6), 1014, 2]]
        assert t["pnl_ts"] == 118 and t["exit_kind"] == "SL"
        # FSMジャーナル全行程
        names = [n for n, _ in t["journal"]]
        assert names == ["PLACE_PROBE", "PROBE_FILL", "ADD_FILL", "MOVE_SL",
                         "SL_TO_BREAKEVEN", "HALF_TAKE_PROFIT", "SL_HIT"]
        # 各退出の種別 (台帳 exits と同順): 1つ目=半分利確, 2つ目=建値SL
        # (一律 "SL" 表示にしない: 表示バグ修正)
        assert t["exit_kinds"] == ["TP", "BE_SL"]
        # プラン根拠ライン (基本フィールド)
        assert t["plan"]["limit"] == 990 and t["plan"]["sl"] == 960
        assert t["plan"]["invalidation"] == 950 and t["plan"]["w1_high"] == 1020
        assert t["plan"]["fib_target"] == 1071
        assert t["plan"]["expiry"] == int(FAR.timestamp())
        # 可視化用の根拠 (make_plan は既定値 → 空)
        assert t["plan"]["fib_levels"] == [] and t["plan"]["sr_zones"] == []
        assert t["plan"]["w1_low"] == 0
        # AIゲートの最終Verdict
        assert t["verdict"]["decision"] == "GO"
        assert t["verdict"]["confidence"] == "0.8"

    def test_unfilled_plan_recorded_with_reason(self):
        # 指値900は到達せず、expiry (bar3 close) で取消される
        plan = make_plan(plan_id="uf1", limit_price_int=900, sl_int=880,
                         invalidation_price=870, expiry=T0 + 3 * BAR)
        report, rec = run_with_recorder({1: ProviderOutput(plans=[plan])})
        assert report.trades == ()
        assert len(rec.unfilled) == 1
        u = rec.unfilled[0]
        assert u["id"] == "uf1" and u["cancel_reason"] == "expired"
        assert u["plan"]["limit"] == 900
        assert u["verdict"]["decision"] == "GO"


class TestClassifyExits:
    def test_runner_close_reasons_distinguished(self):
        # 半分利確 → ランナーがフィボ目標で決済
        fib = [["HALF_TAKE_PROFIT", {}], ["CLOSE_ALL", {"reason": "FIB_TARGET"}]]
        assert classify_exits(fib) == ["TP", "FIB"]
        # 半分利確 → ダウ転換で決済
        dow = [["HALF_TAKE_PROFIT", {}], ["CLOSE_ALL", {"reason": "DOW_REVERSAL"}]]
        assert classify_exits(dow) == ["TP", "DOW"]
        # データ末尾の強制手仕舞い
        eod = [["HALF_TAKE_PROFIT", {}], ["CLOSE_ALL", {"reason": "END_OF_DATA"}]]
        assert classify_exits(eod) == ["TP", "EOD"]

    def test_be_sl_vs_plain_sl(self):
        be = [["SL_TO_BREAKEVEN", {}], ["HALF_TAKE_PROFIT", {}], ["SL_HIT", {}]]
        assert classify_exits(be) == ["TP", "BE_SL"]
        plain = [["SL_HIT", {}]]                          # 建値移動前の損切り
        assert classify_exits(plain) == ["SL"]


class TestReportData:
    def test_money_conversion_exact(self):
        report, rec = run_with_recorder(SCRIPT)
        data = build_report_data(
            candles=CANDLES, report=report, recorder=rec, spec=SPEC,
            tf=Timeframe.M5, initial_capital=Decimal(10_000),
            contract_size=Decimal(10_000))
        s = data["summary"]
        # usd/ts = 0.01 × 0.01 × 10000 = 1 → 118 ts = $118
        assert s["usd_per_tick_step"] == "1.0000"
        assert s["net_profit_usd"] == "118.00"
        assert s["final_equity"] == "10118.00"
        assert s["return_pct"] == "1.18"
        assert s["trades"] == 1 and s["unfilled_plans"] == 0
        assert data["equity"] == [[unix(6), "10118.00"]]
        assert data["monthly"] == {"2026-06": "118.00"}
        t = data["trades"][0]
        assert t["pnl_usd"] == "118.00"
        assert t["entry_time"] == unix(2) and t["exit_time"] == unix(6)
        # R = 118 / (|990-960|×2) = 118/60
        assert t["r_multiple"] == "1.97"

    def test_candles_columnar_roundtrip(self):
        report, rec = run_with_recorder(SCRIPT)
        data = build_report_data(
            candles=CANDLES, report=report, recorder=rec, spec=SPEC,
            tf=Timeframe.M5, initial_capital=Decimal(10_000),
            contract_size=Decimal(100))
        cc = data["candles"]
        assert len(cc["o"]) == len(CANDLES)
        # t_delta の累積 = 各バー open_time の unix 秒
        acc, ts = 0, []
        for d in cc["t_delta"]:
            acc += d
            ts.append(acc)
        assert ts == [int(c.open_time.timestamp()) for c in CANDLES]
        assert cc["c"] == [c.c_int for c in CANDLES]
        assert data["bar_seconds"] == 300


class TestWriteHtml:
    def test_files_written_and_parseable(self, tmp_path):
        report, rec = run_with_recorder(SCRIPT)
        html = write_html_report(
            tmp_path, candles=CANDLES, report=report, recorder=rec,
            spec=SPEC, tf=Timeframe.M5)
        assert html.exists() and html.name == "report.html"
        text = html.read_text(encoding="utf-8")
        assert "lightweight-charts" in text and "report_data.js" in text
        data_js = (tmp_path / "report_data.js").read_text(encoding="utf-8")
        assert data_js.startswith("window.BT = ")
        payload = json.loads(data_js.removeprefix("window.BT = ").rstrip(";\n"))
        assert payload["summary"]["symbol"] == "XAUUSD"
        assert len(payload["trades"]) == 1

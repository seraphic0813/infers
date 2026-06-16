"""ジャーナル永続化 + ゴールデンリプレイ (CLAUDE.md 第11条)。

LiveRunner を SimBroker・合成フィードで駆動し、追記専用 JSONL に判断が
記録されること、再読込でタイムラインが復元できること、ルールゲートの
ゴールデンリプレイ (特徴量→判定の回帰検証) が成立することを検証する。
MT5 への実接続は行わない。
"""

from datetime import timedelta, timezone
from decimal import Decimal

from infers.ai.gateway import (
    AiGateway, EscalationPolicy, JudgementKind, JudgementRequest, VerdictCache,
)
from infers.ai.rule_judge import RuleBasedLlmClient
from infers.analysis.dow import StructureEvent, StructureEventType, TrendState
from infers.analysis.zigzag import SwingPoint
from infers.backtest.engine import LedgerBroker
from infers.core.loop import ProviderOutput, TradePlan
from infers.data.models import Timeframe
from infers.execution.mt5_adapter import LiveRunner
from infers.execution.risk import RiskConfig, RiskManager
from infers.execution.sm import FsmConfig
from infers.journal import JournalWriter, read_journal, replay

from tests.test_integration import CANDLES, FakeFeed, GOLD, T0  # 既存合成系列を再利用

UTC = timezone.utc
FAR = T0 + timedelta(days=30)

# ルールゲートが GO を出せる、実特徴量入りのプラン (rule_judge.§5 を満たす)。
RULE_FEATURES = {
    "dow_state": "UP",
    "limit": "990",
    "invalidation": "950",
    "w1_high": "1020",
    "rsi_band": "20..28",          # 買い: 上端<=30 → 極値確実圏
    "eta_bars": "1-3",
    "ambiguity": "0.5",
    "cluster_score": "3",
    "families": "RSI,SMA,SR",
    "atr": "10",
}


def _rule_plan() -> TradePlan:
    return TradePlan(
        plan_id="jr1", direction=+1, limit_price_int=990, volume_steps=2,
        add_volume_steps=2, sl_int=960, expiry=FAR, invalidation_price=950,
        w1_high_int=1020, fib_target_int=1071,
        request=JudgementRequest(kind=JudgementKind.ENTRY_GATE, symbol="XAUUSD",
                                 direction=+1, features=dict(RULE_FEATURES)),
        cluster_score=Decimal("3"), ambiguity=Decimal("0.5"),
    )


def _hl_event(price: int) -> StructureEvent:
    s1 = SwingPoint(kind="LOW", bar_time=T0, price_int=price - 5, tf=Timeframe.M5,
                    confirmed_at=T0 + timedelta(minutes=5))
    s2 = SwingPoint(kind="LOW", bar_time=T0 + timedelta(minutes=10), price_int=price,
                    tf=Timeframe.M5, confirmed_at=T0 + timedelta(minutes=15))
    return StructureEvent(type=StructureEventType.HL, swing=s2, prev_swing=s1,
                          state_after=TrendState.UP)


class _ScriptedProvider:
    def __init__(self, script):
        self._script = script
        self._i = -1

    def on_candle(self, candle):
        self._i += 1
        return self._script.get(self._i, ProviderOutput())


RULE_POLICY = EscalationPolicy(score_l1=Decimal(2), score_l2=Decimal(4),
                               ambiguity_gray=Decimal("0.1"),
                               l2_daily_call_cap=1_000_000)


def _run_session(journal_path, script) -> JournalWriter:
    broker = LedgerBroker(spread_ticks=2, min_stop_distance_ticks=5)
    journal = JournalWriter(journal_path)
    journal.record("SESSION", {"mode": "live", "symbol": "XAUUSD", "tf": "M5",
                               "ai_client": "rule", "demo": True})
    runner = LiveRunner(
        feed=FakeFeed(CANDLES), spec=GOLD, tf=Timeframe.M5, broker=broker,
        provider=_ScriptedProvider(script),
        gateway=AiGateway(client=RuleBasedLlmClient(), cache=VerdictCache(),
                          policy=RULE_POLICY),
        risk=RiskManager(RiskConfig(max_position_volume_steps=4,
                                    max_total_volume_steps=8, max_spread_ticks=10,
                                    daily_loss_limit_tick_steps=10_000)),
        fsm_config=FsmConfig(min_be_distance_ticks=10, be_offset_ticks=2,
                             breakout_buffer_ticks=10),
        event_source=broker.process_bar, spread_fn=lambda: 2,
        journal=journal,
    )
    runner.run(max_bars=len(CANDLES))
    journal.close()
    return journal


SCRIPT = {1: ProviderOutput(plans=[_rule_plan()]),
          4: ProviderOutput(structure_events=[_hl_event(1025)]),
          5: ProviderOutput(rsi_value=Decimal(75))}


class TestJournalPersistence:
    def test_session_verdict_and_fsm_recorded(self, tmp_path):
        path = tmp_path / "j.jsonl"
        _run_session(path, SCRIPT)
        events = list(read_journal(path))
        kinds = [e.kind for e in events]
        assert kinds[0] == "SESSION"
        assert "VERDICT" in kinds
        assert "FSM" in kinds
        # 打診→約定→追撃→建値SL→半分利確→SL退出 の遷移が永続化されている
        transitions = [e.data["transition"] for e in events if e.kind == "FSM"
                       if e.data["position_id"] == "jr1"]
        assert "PLACE_PROBE" in transitions
        assert "PROBE_FILL" in transitions
        assert "SL_HIT" in transitions or "CLOSE_ALL" in transitions

    def test_verdict_carries_feature_snapshot(self, tmp_path):
        path = tmp_path / "j.jsonl"
        _run_session(path, SCRIPT)
        verdicts = [e for e in read_journal(path) if e.kind == "VERDICT"]
        assert verdicts
        v = verdicts[0]
        assert v.data["decision"] == "GO"
        assert v.data["features"]["dow_state"] == "UP"      # 特徴量スナップショット (第11条)
        assert v.data["direction"] == 1

    def test_append_only_across_sessions(self, tmp_path):
        """同一パスへの再オープンは追記であり、過去行を破壊しない。"""
        path = tmp_path / "j.jsonl"
        _run_session(path, SCRIPT)
        first = list(read_journal(path))
        _run_session(path, SCRIPT)
        second = list(read_journal(path))
        assert len(second) == 2 * len(first)               # 追記されている
        assert [e.kind for e in second[:len(first)]] == [e.kind for e in first]

    def test_bar_anchor_is_set(self, tmp_path):
        path = tmp_path / "j.jsonl"
        _run_session(path, SCRIPT)
        verdicts = [e for e in read_journal(path) if e.kind == "VERDICT"]
        assert verdicts[0].bar_time is not None             # 確定足にアンカーされている


class TestGoldenReplay:
    def test_rule_session_replays_identically(self, tmp_path):
        """記録済み特徴量を現在の judge_features に再投入 → 全件一致 (回帰なし)。"""
        path = tmp_path / "j.jsonl"
        _run_session(path, SCRIPT)
        result = replay(path)
        assert result.ai_client == "rule"
        assert result.checked >= 1
        assert result.ok                                    # mismatch ゼロ
        assert result.mismatches == ()

    def test_tampered_decision_is_detected(self, tmp_path):
        """記録 decision を改竄すると回帰検証が不一致を検出する (検証の有効性)。"""
        import json
        path = tmp_path / "j.jsonl"
        _run_session(path, SCRIPT)
        lines = path.read_text(encoding="utf-8").splitlines()
        out = []
        for line in lines:
            obj = json.loads(line)
            if obj["kind"] == "VERDICT" and obj["data"]["decision"] == "GO":
                obj["data"]["decision"] = "NO_GO"           # 改竄
            out.append(json.dumps(obj))
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        result = replay(path)
        assert not result.ok
        assert any(rec == "NO_GO" and got == "GO"
                   for _seq, rec, got in result.mismatches)

    def test_replay_from_timestamp_filters(self, tmp_path):
        path = tmp_path / "j.jsonl"
        _run_session(path, SCRIPT)
        full = replay(path)
        late = replay(path, from_ts=T0 + timedelta(days=999))   # 全イベントより後
        assert late.total_events < full.total_events

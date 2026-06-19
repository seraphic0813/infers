"""Phase A 計装リプレイ — ファネル定量化 + 全プラン詳細ダンプ + FSMジャーナル収集。

run_backtest と同一の構成 (CacheOnlyClient / LedgerBroker / DEFAULT_*) で
全期間をリプレイしながら、以下を analysis/ 配下へ書き出す:

  - plans_dump.jsonl   : 発行された全プラン (価格・SL・expiry・特徴量・tier・cache_key)
  - fsm_journals.json  : 発注に至ったプランの FSM ジャーナル全文
  - funnel_summary.json: ダウ状態滞在バー数・プラン数・gateway集計・リスク拒否・トレード結果
"""
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import argparse

from infers.ai.gateway import cache_key
from infers.backtest.engine import LedgerBroker, TradeRecord, build_report
from infers.core.loop import TradingLoop
from infers.data.exporter import load_history
from infers.core.models import Timeframe
from infers.execution.risk import RiskManager
from infers.main import (
    DEFAULT_FSM, DEFAULT_POLICY, DEFAULT_RISK, _build_gateway, build_provider,
)

OUT_DIR = Path(__file__).resolve().parent


class RecordingRisk(RiskManager):
    """approve の拒否を観測するラッパー (挙動は不変)。"""

    def __init__(self, config):
        super().__init__(config)
        self.rejections: Counter = Counter()
        self.approvals = 0

    def approve(self, req, *, current_spread_ticks, open_total_volume_steps):
        verdict = super().approve(
            req, current_spread_ticks=current_spread_ticks,
            open_total_volume_steps=open_total_volume_steps)
        if verdict.approved:
            self.approvals += 1
        else:
            self.rejections[verdict.reason] += 1
        return verdict


def main() -> None:
    args = argparse.Namespace(
        data="data/xauusd_m5.parquet", tf="M5", symbol="XAUUSD",
        provider=None, verdict_cache="verdicts.sqlite3",
    )
    candles = load_history(args.data, tf=Timeframe(args.tf))
    provider = build_provider(args)
    gateway = _build_gateway(args, cache_only=True)
    broker = LedgerBroker(spread_ticks=2, min_stop_distance_ticks=5)
    risk = RecordingRisk(DEFAULT_RISK)
    loop = TradingLoop(broker=broker, gateway=gateway, risk=risk,
                       fsm_config=DEFAULT_FSM)

    dow_bars: Counter = Counter()
    n_plans = 0
    plans_path = OUT_DIR / "plans_dump.jsonl"
    journals: dict[str, list] = {}
    trades: list[TradeRecord] = []
    total = len(candles)

    def harvest(pid: str) -> None:
        """クローズ済みポジションの台帳→TradeRecord 化 (engine._finalize 相当)。"""
        led = broker.ledgers.get(pid)
        if led is None or not led.entries or not led.exits:
            broker.ledgers.pop(pid, None)
            return
        entry_value = sum(p * v for p, v in led.entries)
        exit_value = sum(p * v for p, v in led.exits)
        pnl = led.direction * (exit_value - entry_value)
        trades.append(TradeRecord(position_id=pid, direction=led.direction,
                                  pnl_tick_steps=pnl, exit_kind=led.exit_kind))
        risk.record_realized(pnl)
        del broker.ledgers[pid]

    with plans_path.open("w", encoding="utf-8") as plans_out:
        for i, candle in enumerate(candles):
            if i % 20000 == 0:
                print(f"\r{i}/{total}", end="", file=sys.stderr)
            loop.on_broker_events(broker.process_bar(candle))
            output = provider.on_candle(candle)
            dow_bars[provider._dow.state.name] += 1
            for plan in output.plans:
                n_plans += 1
                plans_out.write(json.dumps({
                    "plan_id": plan.plan_id,
                    "bar_index": i,
                    "direction": plan.direction,
                    "limit": plan.limit_price_int,
                    "sl": plan.sl_int,
                    "expiry": plan.expiry.isoformat(),
                    "invalidation": plan.invalidation_price,
                    "w1_high": plan.w1_high_int,
                    "fib_target": plan.fib_target_int,
                    "cluster_score": str(plan.cluster_score),
                    "ambiguity": str(plan.ambiguity),
                    "features": plan.request.features,
                    "l1_key": cache_key(plan.request, "L1"),
                    "l2_key": cache_key(plan.request, "L2"),
                    "tier": DEFAULT_POLICY.decide(
                        plan.cluster_score, plan.ambiguity).name,
                }, ensure_ascii=False) + "\n")
            # クローズ前に FSM 参照を確保 (closed 後は loop から消える)
            live_refs = {pid: fsm for pid, (fsm, _) in loop.open_positions.items()}
            for pid in loop.on_candle(candle, output, spread_ticks=broker.spread):
                journals[pid] = live_refs[pid].journal
                harvest(pid)
        print(file=sys.stderr)

        # データ末尾の強制クローズ (replay と同一)
        live_refs = {pid: fsm for pid, (fsm, _) in loop.open_positions.items()}
        for pid in loop.close_all_open("END_OF_DATA"):
            journals[pid] = live_refs[pid].journal
            harvest(pid)

    report = build_report(trades)

    (OUT_DIR / "fsm_journals.json").write_text(
        json.dumps(journals, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    (OUT_DIR / "funnel_summary.json").write_text(json.dumps({
        "bars": total,
        "dow_state_bars": dict(dow_bars),
        "plans_issued": n_plans,
        "gateway_stats": dict(gateway.stats),
        "guardrail_reasons": dict(gateway.guardrail_reasons),
        "risk_approvals": risk.approvals,
        "risk_rejections": dict(risk.rejections),
        "orders_placed": len(journals),
        "trades": [
            {"pid": t.position_id, "dir": t.direction,
             "pnl": t.pnl_tick_steps, "exit": t.exit_kind}
            for t in report.trades],
        "total_pnl": report.total_pnl_tick_steps,
        "max_dd": report.max_drawdown_tick_steps,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print("done:", plans_path, OUT_DIR / "funnel_summary.json")


if __name__ == "__main__":
    main()

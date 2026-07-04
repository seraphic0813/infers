"""ライブジャーナルのSL/トレール決済: 実約定 vs 期待値の乖離集計(改善プランT8)。

atr_trend_scalp の `trail_atr_mult=0.15`(≈6ティック)は約定現実性の下限に近く、
バックテスト(SimBroker)は「SL/トレール価格ちょうどで滑りなく約定する」前提を置く。
本ツールはライブ(デモ)のジャーナルから実際の約定価格(`SL_HIT` イベントの `price`。
`execution/mt5_adapter.py` の `poll_events()` が MT5 の実約定 `deal.price` をそのまま
記録している)と、その時点で有効だった SL/トレール価格(直近の `MOVE_SL` の `new_sl`。
無ければ `MARKET_ENTRY` の `sl`)を突合し、乖離(ticks)の分布を算出する。

**トレール(RUNNER状態=TP1到達後)関連の SL_HIT と、初期SL(OPEN状態=TP1未到達)の
SL_HIT を分けて集計する**(前者が T7 のスリッページ・ストレステストへのフィードバック
対象。トレール判定は「同一 position_id で `TP_CLOSE(level=tp1)` が先行するか」で行う。
`SL_HIT` イベント自体の `state` フィールドは既に CLOSED のため使えない)。

トレード判定コードには一切触れない(ジャーナルの読み取りのみ)。

使い方:
    python tools/atr_demo_deviation.py work/journal/atr_trend_scalp_xauusd.jsonl
    python tools/atr_demo_deviation.py <journal> --tick-value-usd 0.01 --json
"""

from __future__ import annotations

import argparse
import statistics
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from infers.journal import read_journal

# T7(スリッページ・ストレステスト)で実測PASSを確認した範囲(--slippage-ticks 0..3)。
# 観測 p90 がこの範囲内なら T7 の感度テーブルからそのまま PF を読める。
T7_TESTED_MAX_TICKS = 3


@dataclass
class _PositionState:
    direction: int | None = None
    current_sl: int | None = None
    reached_runner: bool = False


@dataclass(frozen=True)
class Deviation:
    position_id: str
    category: str          # "trail" (RUNNER状態でのSL_HIT) | "initial_stop" (OPEN状態)
    expected_sl: int
    actual_fill: int
    deviation_ticks: int    # 正 = 不利(想定より悪い価格で約定。SimBrokerのslippage_ticksと同じ符号規約)


def compute_deviations(path: str | Path) -> list[Deviation]:
    """ジャーナルを1回走査し、position_id ごとに直近の有効SLを追跡しながら
    SL_HIT のたびに乖離を記録する。"""
    positions: dict[str, _PositionState] = {}
    deviations: list[Deviation] = []
    for ev in read_journal(path):
        if ev.kind != "FSM":
            continue
        pid = ev.data.get("position_id")
        if pid is None:
            continue
        transition = ev.data.get("transition")
        st = positions.setdefault(pid, _PositionState())

        if transition == "MARKET_ENTRY":
            fill = int(ev.data["fill"])
            sl = int(ev.data["sl"])
            st.current_sl = sl
            st.direction = 1 if fill > sl else -1
        elif transition == "MOVE_SL":
            st.current_sl = int(ev.data["new_sl"])
        elif transition == "TP_CLOSE" and ev.data.get("level") == "tp1":
            st.reached_runner = True
        elif transition == "SL_HIT":
            if st.direction is None or st.current_sl is None:
                continue  # 不完全なジャーナル(途中からの記録開始等)はスキップ
            actual = int(ev.data["price"])
            expected = st.current_sl
            deviation = st.direction * (expected - actual)
            category = "trail" if st.reached_runner else "initial_stop"
            deviations.append(Deviation(
                position_id=str(pid), category=category,
                expected_sl=expected, actual_fill=actual, deviation_ticks=deviation))
    return deviations


def _percentile(values: list[int], pct: float) -> int:
    """簡易パーセンタイル(線形補間なしの最近接下位、n小でも安定)。"""
    idx = min(len(values) - 1, max(0, int(round(pct * (len(values) - 1)))))
    return values[idx]


def summarize(deviations: list[Deviation], category: str) -> dict:
    vals = sorted(d.deviation_ticks for d in deviations if d.category == category)
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "median_ticks": statistics.median(vals),
        "p90_ticks": _percentile(vals, 0.90),
        "min_ticks": vals[0],
        "max_ticks": vals[-1],
        "mean_ticks": round(statistics.fmean(vals), 3),
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", type=Path, help="ライブジャーナル(JSONL)のパス")
    parser.add_argument("--tick-value-usd", type=str, default="0.01",
                        help="1tickのUSD換算値(XAUUSD既定0.01)")
    parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    args = parser.parse_args(argv)

    if not args.journal.exists():
        print(f"error: journal not found: {args.journal}", file=sys.stderr)
        return 2

    deviations = compute_deviations(args.journal)
    tick_usd = Decimal(args.tick_value_usd)
    trail = summarize(deviations, "trail")
    initial = summarize(deviations, "initial_stop")

    if args.json:
        import json
        print(json.dumps({"trail": trail, "initial_stop": initial}, ensure_ascii=False, indent=2))
        return 0

    print(f"journal: {args.journal}")
    print(f"SL_HIT件数: trail(RUNNER後)={trail.get('n', 0)}  "
          f"initial_stop(OPEN中)={initial.get('n', 0)}")
    print()
    for label, stats in (("トレール関連(T7フィードバック対象)", trail),
                         ("初期SL(参考)", initial)):
        print(f"## {label}")
        if stats["n"] == 0:
            print("  データなし(該当イベントが0件)")
            continue
        p90 = stats["p90_ticks"]
        p90_usd = Decimal(p90) * tick_usd
        print(f"  n={stats['n']}  中央値={stats['median_ticks']:.1f}tick  "
              f"p90={p90}tick(${p90_usd})  範囲=[{stats['min_ticks']}, {stats['max_ticks']}]tick")
        if label.startswith("トレール"):
            if p90 <= T7_TESTED_MAX_TICKS:
                print(f"  → p90({p90}tick) は T7 実測範囲(0〜{T7_TESTED_MAX_TICKS}tick)内。"
                      f"health_check.md の該当Nの実測PFをそのまま最終判定値に使える。")
            else:
                print(f"  → p90({p90}tick) が T7 実測範囲(0〜{T7_TESTED_MAX_TICKS}tick)を超過。"
                      f"--slippage-ticks {p90} 以上でT7スイープを再実行し、"
                      f"PFが1.2を維持するか再判定が必要。")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())

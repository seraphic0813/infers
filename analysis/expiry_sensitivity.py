"""expiry 感度分析 — 失効した指値の expiry を延長したら何が起きたか。

counterfactual.py と同一の簡易執行で、expiry を k_max×{1,2,3,6}倍・無限に
変えて約定率と成績の変化を測る。RSI系 (深い指値) の救済効果が焦点。
"""
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from infers.data.exporter import load_history
from infers.core.models import Timeframe

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = Path(__file__).resolve().parent
SPREAD = 2
BAR = timedelta(minutes=5)


def simulate(plans, candles, expiry_mult):
    n = len(candles)
    h = [c.h_int for c in candles]
    l = [c.l_int for c in candles]  # noqa: E741
    c_ = [c.c_int for c in candles]
    close_time = [c.close_time for c in candles]
    last_close = c_[-1]

    out = []
    for p in plans:
        i0 = p["bar_index"]
        d = p["direction"]
        limit = p["limit"]
        sl = p["sl"]
        target = p["fib_target"]
        inv = p["invalidation"]
        k_max = int(p["features"]["eta_bars"].split("-")[1])
        expiry = (None if expiry_mult is None
                  else close_time[i0] + k_max * expiry_mult * BAR)
        risk_ticks = abs(limit - sl)

        fill_bar = None
        cancel = None
        j = i0 + 1
        while j < n:
            touched = (l[j] + SPREAD <= limit) if d > 0 else (h[j] - SPREAD >= limit)
            if touched:
                fill_bar = j
                break
            if expiry is not None and close_time[j] >= expiry:
                cancel = "expired"
                break
            if d * (c_[j] - inv) < 0:
                cancel = "invalidated"
                break
            j += 1
        if fill_bar is None:
            out.append({**p, "outcome": cancel or "end_of_data", "r": None})
            continue

        outcome, r = None, None
        k = fill_bar
        while k < n:
            if (l[k] <= sl) if d > 0 else (h[k] >= sl):
                outcome = "sl"
                r = Decimal(d * (sl - limit)) / risk_ticks
                break
            if (h[k] >= target) if d > 0 else (l[k] <= target):
                outcome = "target"
                r = Decimal(d * (target - limit)) / risk_ticks
                break
            k += 1
        if outcome is None:
            outcome = "open_at_end"
            r = Decimal(d * (last_close - limit)) / risk_ticks
        out.append({**p, "outcome": outcome, "r": r})
    return out


def main() -> None:
    plans = [json.loads(ln) for ln in
             (OUT_DIR / "plans_dump.jsonl").read_text(encoding="utf-8").splitlines()]
    candles = load_history(ROOT / "data" / "xauusd_m5.parquet", tf=Timeframe("M5"))
    conn = sqlite3.connect(ROOT / "verdicts.sqlite3")
    verdicts = {k: json.loads(v) for k, v in
                conn.execute("SELECT cache_key, verdict_json FROM verdicts")}

    def vdec(p):
        v2 = verdicts.get(p["l2_key"])
        v1 = verdicts.get(p["l1_key"])
        if p["tier"] == "L2_AFTER_L1" and v1 and v1["decision"] == "GO" and v2:
            return f"L2:{v2['decision']}"
        return f"L1:{v1['decision']}" if v1 else "UNRESOLVED"

    def rsi_grp(p):
        return "RSI系" if "RSI" in p["features"]["families"] else "非RSI"

    for mult in (1, 2, 3, 6, None):
        results = simulate(plans, candles, mult)
        label = f"expiry×{mult}" if mult else "expiry∞(inv失効のみ)"
        print(f"\n===== {label} =====")
        groups = defaultdict(list)
        for p in results:
            groups[("ALL",)].append(p)
            groups[(vdec(p),)].append(p)
            groups[(rsi_grp(p),)].append(p)
        for key in [("ALL",), ("L2:GO",), ("L1:GO",), ("L2:WAIT",),
                    ("L1:WAIT",), ("L1:NO_GO",), ("RSI系",), ("非RSI",)]:
            items = groups.get(key, [])
            if not items:
                continue
            filled = [p for p in items if p["r"] is not None]
            closed = [p for p in filled if p["outcome"] in ("sl", "target")]
            wins = sum(1 for p in closed if p["outcome"] == "target")
            opens = [p for p in filled if p["outcome"] == "open_at_end"]
            avg_r = (sum(p["r"] for p in closed) / len(closed)) if closed else Decimal(0)
            sum_r = sum(p["r"] for p in closed) if closed else Decimal(0)
            print(f"  {key[0]:8s}: fill={len(filled):5d}/{len(items):5d} "
                  f"({len(filled)/len(items)*100:4.1f}%) win={wins:4d}"
                  f"/{len(closed):5d} ({wins/len(closed)*100 if closed else 0:5.1f}%) "
                  f"avgR={avg_r:+.3f} sumR={sum_r:+9.1f} open={len(opens)}")


if __name__ == "__main__":
    main()

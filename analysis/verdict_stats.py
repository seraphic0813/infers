"""L1/L2 判定の reasons 集計 + features×decision 結合統計。

バッチ入力 JSONL (custom_id → features) と verdicts.sqlite3
(cache_key → decision/confidence/reasons) を結合して分析する。
"""
import json
import re
import sqlite3
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

L1_FILES = ["pilot_L1.jsonl", "pilot_L1_1000.jsonl", "batch_L1_5000.jsonl",
            "batch_L1_6000.jsonl", "batch_L1_rest.jsonl"]
L2_FILES = ["pilot_L2.jsonl", "batch_L2.jsonl"]


def load_requests(files: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name in files:
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            obj = json.loads(line)
            msg = json.loads(obj["params"]["messages"][0]["content"])
            out[obj["custom_id"]] = {
                "features": msg["features"], "direction": msg["direction"]}
    return out


def load_verdicts() -> dict[str, dict]:
    conn = sqlite3.connect(ROOT / "verdicts.sqlite3")
    rows = conn.execute("SELECT cache_key, verdict_json FROM verdicts").fetchall()
    return {k: json.loads(v) for k, v in rows}


def fnum(s: str) -> Decimal:
    return Decimal(s)


def band(s: str) -> tuple[Decimal, Decimal]:
    lo, hi = s.split("..")
    return Decimal(lo), Decimal(hi)


def eta(s: str) -> tuple[int, int]:
    a, b = s.split("-")
    return int(a), int(b)


def bucket(x: Decimal, edges: list[str]) -> str:
    for e in edges:
        if x < Decimal(e):
            return f"<{e}"
    return f">={edges[-1]}"


def main() -> None:
    reqs = load_requests(L1_FILES)
    l2_reqs = load_requests(L2_FILES)
    verdicts = load_verdicts()

    # ---- 1) decision 分布と reasons 頻出パターン --------------------------------
    print("=" * 70)
    print("L1 reasons 頻出分析 (判定別)")
    by_dec_reasons: dict[str, Counter] = defaultdict(Counter)
    by_dec: Counter = Counter()
    for cid in reqs:
        v = verdicts.get(cid)
        if v is None:
            continue
        by_dec[v["decision"]] += 1
        for r in v.get("reasons", []):
            # 数値を除去してパターン化 (例 "ambiguity=0.41が高く" → "ambiguity=#が高く")
            pat = re.sub(r"\d+(?:\.\d+)?", "#", r)[:80]
            by_dec_reasons[v["decision"]][pat] += 1
    print("decision 分布:", dict(by_dec))
    for dec in ("NO_GO", "WAIT", "GO"):
        print(f"\n--- {dec} の reasons TOP15 ---")
        for pat, n in by_dec_reasons[dec].most_common(15):
            print(f"  {n:6d}  {pat}")

    # ---- 2) features×decision 統計 ----------------------------------------------
    print("\n" + "=" * 70)
    print("features 別 decision 分布 (L1)")

    dims: dict[str, dict[str, Counter]] = {
        "cluster_score": defaultdict(Counter),
        "ambiguity": defaultdict(Counter),
        "families": defaultdict(Counter),
        "rsi_band_ok": defaultdict(Counter),     # 方向に対しRSI根拠が成立してるか
        "rr_ok": defaultdict(Counter),           # reward_ref >= risk
        "eta_width": defaultdict(Counter),
        "band_degenerate": defaultdict(Counter), # lo==hi
        "direction": defaultdict(Counter),
    }
    for cid, req in reqs.items():
        v = verdicts.get(cid)
        if v is None:
            continue
        dec = v["decision"]
        f = req["features"]
        d = req["direction"]
        cs = fnum(f["cluster_score"])
        amb = fnum(f["ambiguity"])
        lo, hi = band(f["rsi_band"])
        k_min, k_max = eta(f["eta_bars"])
        limit = int(f["limit"]); inv = int(f["invalidation"]); w1 = int(f["w1_high"])
        risk = abs(limit - inv); reward = abs(w1 - limit)

        dims["cluster_score"][bucket(cs, ["2.5", "3", "3.5", "4"])][dec] += 1
        dims["ambiguity"][bucket(amb, ["0.05", "0.1", "0.2", "0.3"])][dec] += 1
        dims["families"][f["families"]][dec] += 1
        rsi_ok = (lo <= 30) if d > 0 else (hi >= 70)
        rsi_certain = (hi <= 30) if d > 0 else (lo >= 70)
        dims["rsi_band_ok"]["certain" if rsi_certain else ("possible" if rsi_ok else "no")][dec] += 1
        dims["rr_ok"]["reward>=risk" if reward >= risk else
                      ("reward>=0.5risk" if reward * 2 >= risk else "reward<0.5risk")][dec] += 1
        dims["eta_width"][f"k_max={k_max}" if k_max < 12 else "k_max=12"][dec] += 1
        dims["band_degenerate"]["lo==hi" if lo == hi else "lo<hi"][dec] += 1
        dims["direction"]["LONG" if d > 0 else "SHORT"][dec] += 1

    for dim, table in dims.items():
        print(f"\n--- {dim} ---")
        for key in sorted(table, key=str):
            c = table[key]
            tot = sum(c.values())
            go = c.get("GO", 0); wait = c.get("WAIT", 0); nogo = c.get("NO_GO", 0)
            print(f"  {key:>18}: n={tot:6d}  GO={go:3d} ({go/tot*100:5.2f}%)  "
                  f"WAIT={wait:5d} ({wait/tot*100:5.1f}%)  NO_GO={nogo:5d} ({nogo/tot*100:5.1f}%)")

    # ---- 3) L2 詳細 ----------------------------------------------------------------
    print("\n" + "=" * 70)
    print("L2 全19件の詳細")
    for cid, req in l2_reqs.items():
        v = verdicts.get(cid)
        if v is None:
            continue
        f = req["features"]
        print(f"  {v['decision']:5s} conf={v['confidence']:>5} "
              f"dir={req['direction']:+d} cs={f['cluster_score']} amb={f['ambiguity']} "
              f"fam={f['families']} band={f['rsi_band']} eta={f['eta_bars']}")
        for r in v.get("reasons", []):
            print(f"          - {r[:100]}")

    # ---- 4) GO 34件 (L1) の特徴 -----------------------------------------------------
    print("\n" + "=" * 70)
    print("L1 GO 34件の特徴")
    for cid, req in reqs.items():
        v = verdicts.get(cid)
        if v is None or v["decision"] != "GO":
            continue
        f = req["features"]
        print(f"  conf={v['confidence']:>5} dir={req['direction']:+d} "
              f"cs={f['cluster_score']} amb={f['ambiguity']:>12} fam={f['families']:16s} "
              f"band={f['rsi_band']} eta={f['eta_bars']}")


if __name__ == "__main__":
    main()

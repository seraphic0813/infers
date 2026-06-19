"""反実仮想シミュレーション — 全プランを「もしエントリーしていたら」で評価する。

LLMゲートの判定 (GO/WAIT/NO_GO) とは独立に、全18,070プランへ同一の
簡易執行ルールを適用し、判定群別・特徴量別の成績を比較する。
これにより「LLMゲートに選別能力があるか」「どの特徴量に予測力があるか」を
直接測定できる (プロンプト nf-v2 の ambiguity 誤定義の影響検証を含む)。

簡易執行ルール (SimBroker / PositionFSM の约定経路を再現、ただし
建値SL移動・半分利確・追撃は再現しない):
  1. プラン発行バーの翌バーから: process_bar 相当の約定判定
     (買い: l+spread <= limit) → 約定。同バーで約定しなければ
     on_bar_pending 相当 (close_time >= expiry / 終値が invalidation 抵触)
     でキャンセル。
  2. 約定後: 毎バー SL 判定 (買い: l <= sl。保守側で SL 先勝ち) と
     fib_target 判定 (買い: h >= target) の先着。
  3. データ終端なら最終終値で時価評価 (open扱い)。

成績は R 倍数 (= pnl_ticks / |limit - sl|) で正規化して比較する。
"""
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from infers.data.exporter import load_history
from infers.core.models import Timeframe

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = Path(__file__).resolve().parent
SPREAD = 2


def simulate(plans: list[dict], candles) -> list[dict]:
    n = len(candles)
    h = [c.h_int for c in candles]
    l = [c.l_int for c in candles]  # noqa: E741
    c_ = [c.c_int for c in candles]
    close_time = [c.close_time for c in candles]
    last_close = c_[-1]

    results = []
    for p in plans:
        i0 = p["bar_index"]
        d = p["direction"]
        limit = p["limit"]
        sl = p["sl"]
        target = p["fib_target"]
        inv = p["invalidation"]
        expiry = datetime.fromisoformat(p["expiry"])
        risk_ticks = abs(limit - sl)

        # --- 1) 約定走査 (翌バーから) ---
        fill_bar = None
        cancel = None
        j = i0 + 1
        while j < n:
            touched = (l[j] + SPREAD <= limit) if d > 0 else (h[j] - SPREAD >= limit)
            if touched:
                fill_bar = j
                break
            if close_time[j] >= expiry:
                cancel = "expired"
                break
            if d * (c_[j] - inv) < 0:
                cancel = "invalidated"
                break
            j += 1
        if fill_bar is None:
            results.append({**p, "outcome": cancel or "end_of_data",
                            "r": None, "bars_held": 0})
            continue

        # --- 2) SL / target 先着走査 (約定バー含む: SimBroker保守則で同バーSL可) ---
        outcome = None
        r = None
        k = fill_bar
        while k < n:
            sl_hit = (l[k] <= sl) if d > 0 else (h[k] >= sl)
            if sl_hit:
                outcome = "sl"
                r = Decimal(d * (sl - limit)) / risk_ticks
                break
            tgt_hit = (h[k] >= target) if d > 0 else (l[k] <= target)
            if tgt_hit:
                outcome = "target"
                r = Decimal(d * (target - limit)) / risk_ticks
                break
            k += 1
        if outcome is None:
            outcome = "open_at_end"
            r = Decimal(d * (last_close - limit)) / risk_ticks
            k = n - 1
        results.append({**p, "outcome": outcome,
                        "r": str(r.quantize(Decimal('0.001'))),
                        "bars_held": k - fill_bar})
    return results


def summarize(results: list[dict], verdicts: dict[str, dict]) -> None:
    def vdec(p: dict) -> str:
        # L2解決済みならL2判定を優先 (ゲートウェイの実効判定に一致)
        v2 = verdicts.get(p["l2_key"])
        v1 = verdicts.get(p["l1_key"])
        if p["tier"] == "L2_AFTER_L1" and v1 and v1["decision"] == "GO" and v2:
            return f"L2:{v2['decision']}"
        if v1:
            return f"L1:{v1['decision']}"
        return "UNRESOLVED"

    groups: dict[str, list[dict]] = defaultdict(list)
    for p in results:
        groups[vdec(p)].append(p)

    def stat_line(tag: str, items: list[dict]) -> None:
        filled = [p for p in items if p["r"] is not None]
        closed = [p for p in filled if p["outcome"] in ("sl", "target")]
        if not filled:
            print(f"  {tag:12s}: n={len(items):6d} filled=0")
            return
        rs = [Decimal(p["r"]) for p in closed]
        wins = sum(1 for p in closed if p["outcome"] == "target")
        open_rs = [Decimal(p["r"]) for p in filled if p["outcome"] == "open_at_end"]
        avg_r = sum(rs) / len(rs) if rs else Decimal(0)
        print(f"  {tag:12s}: n={len(items):6d} filled={len(filled):5d} "
              f"({len(filled)/len(items)*100:4.1f}%) closed={len(closed):5d} "
              f"win={wins:4d} ({wins/len(closed)*100 if closed else 0:5.1f}%) "
              f"avgR(closed)={avg_r:+.3f} open_at_end={len(open_rs)}"
              + (f" avgR(open)={sum(open_rs)/len(open_rs):+.2f}" if open_rs else ""))

    print("=" * 100)
    print("判定群別の反実仮想成績 (全プラン共通の簡易執行: 固定SL vs fib_target 先着)")
    for tag in sorted(groups):
        stat_line(tag, groups[tag])

    # --- ambiguity 方向の検証 (プロンプト誤定義の影響測定) ---
    print()
    print("ambiguity (=1位2位スコア差) バケット別 — コード意味論: 大=解釈一意")
    buckets: dict[str, list[dict]] = defaultdict(list)
    for p in results:
        a = Decimal(p["ambiguity"])
        if a < Decimal("0.05"):
            b = "a<0.05 (拮抗=曖昧)"
        elif a < Decimal("0.1"):
            b = "0.05-0.1"
        elif a < Decimal("0.3"):
            b = "0.1-0.3"
        elif a < Decimal("1"):
            b = "0.3-1"
        else:
            b = "=1 (候補単一)"
        buckets[b].append(p)
    for b in ["a<0.05 (拮抗=曖昧)", "0.05-0.1", "0.1-0.3", "0.3-1", "=1 (候補単一)"]:
        stat_line(b, buckets.get(b, []))

    # --- cluster_score / families / direction / rsi成立 ---
    for dim, keyf in [
        ("cluster_score", lambda p: f"cs={p['cluster_score'][:3]}"),
        ("direction", lambda p: "LONG" if p["direction"] > 0 else "SHORT"),
        ("families", lambda p: p["features"]["families"]),
    ]:
        print(f"\n{dim} 別")
        buckets = defaultdict(list)
        for p in results:
            buckets[keyf(p)].append(p)
        for b in sorted(buckets):
            stat_line(b, buckets[b])


def main() -> None:
    plans = [json.loads(ln) for ln in
             (OUT_DIR / "plans_dump.jsonl").read_text(encoding="utf-8").splitlines()]
    candles = load_history(ROOT / "data" / "xauusd_m5.parquet", tf=Timeframe("M5"))
    conn = sqlite3.connect(ROOT / "verdicts.sqlite3")
    verdicts = {k: json.loads(v) for k, v in
                conn.execute("SELECT cache_key, verdict_json FROM verdicts")}

    print(f"plans={len(plans)} bars={len(candles)}", file=sys.stderr)
    results = simulate(plans, candles)
    (OUT_DIR / "counterfactual_results.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in results)
        + "\n", encoding="utf-8")
    summarize(results, verdicts)


if __name__ == "__main__":
    main()

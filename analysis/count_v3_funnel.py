"""nf-v3 / 新プロバイダ (P4/P5/P6) 下のファネル集計とコスト試算。

instrumented_replay.py が書き出した plans_dump.jsonl を読み、
  - プラン総数・tier分布
  - 再ジャッジに必要な一意 feature_hash 数 (L1 / L2)
  - P4/P5/P6 の効果検証 (rsi_band 退化率・eta_bars 幅・families分布)
を出力し、L1/L2 実測単価で全件コストを試算する。
"""
import json
from collections import Counter
from pathlib import Path

OUT = Path(__file__).resolve().parent
PLANS = OUT / "plans_dump.jsonl"

# 実測単価 (USD/件)。L1=Haiku, L2=Fable5。会話で確定した値。
PRICE_L1 = 0.00108
PRICE_L2 = 0.0544


def main() -> None:
    plans = [json.loads(ln) for ln in
             PLANS.read_text(encoding="utf-8").splitlines() if ln.strip()]
    n = len(plans)
    print(f"=== nf-v3 funnel (plans_dump.jsonl) ===")
    print(f"plans_issued: {n}")

    tier = Counter(p["tier"] for p in plans)
    print(f"\ntier 分布: {dict(tier)}")

    # 再ジャッジ対象の一意キー
    escalated = [p for p in plans if p["tier"] != "NONE"]
    l2_plans = [p for p in plans if p["tier"] == "L2_AFTER_L1"]
    uniq_l1 = {p["l1_key"] for p in escalated}
    uniq_l2 = {p["l2_key"] for p in l2_plans}
    print(f"\n再ジャッジ一意キー:")
    print(f"  L1 (escalated 全体): {len(uniq_l1)} 件")
    print(f"  L2 (L2_AFTER_L1):    {len(uniq_l2)} 件")

    # families 分布 (P6: SR,FIB のみが消えているか)
    fam = Counter(p["features"]["families"] for p in plans)
    core_missing = [f for f in fam if "SMA" not in f and "RSI" not in f]
    print(f"\nfamilies 上位:")
    for f, c in fam.most_common(8):
        print(f"  {f:20s}: {c}")
    print(f"  中核根拠(SMA/RSI)欠如の families: {core_missing or 'なし (P6 有効)'}")

    # eta_bars 幅 (P5: 1-12 全域が支配的でないか)
    def width(p):
        lo, hi = p["features"]["eta_bars"].split("-")
        return int(hi) - int(lo)
    eta_full = sum(1 for p in plans if p["features"]["eta_bars"] in ("1-12",))
    wdist = Counter(width(p) for p in plans)
    print(f"\neta_bars:")
    print(f"  '1-12' (全域) の割合: {eta_full}/{n} = {eta_full/n*100:.1f}%")
    print(f"  幅の分布(上位): {dict(sorted(wdist.items())[:13])}")

    # rsi_band 退化 (P4: lo==hi の点退化率)
    def degen(p):
        lo, hi = p["features"]["rsi_band"].split("..")
        return lo == hi
    nd = sum(1 for p in plans if degen(p))
    print(f"\nrsi_band 点退化 (lo==hi): {nd}/{n} = {nd/n*100:.1f}%")

    # コスト試算 (PROMPT_VERSION 変更で全件再ジャッジ)
    print(f"\n=== コスト試算 (全件再ジャッジ) ===")
    c_l1 = len(uniq_l1) * PRICE_L1
    c_l2 = len(uniq_l2) * PRICE_L2
    print(f"  現行2段 (L1 {len(uniq_l1)}×${PRICE_L1} + L2 {len(uniq_l2)}×${PRICE_L2}):")
    print(f"      L1=${c_l1:.2f}  L2=${c_l2:.2f}  合計=${c_l1+c_l2:.2f}")
    # L1廃止・L2直結の場合 (escalated 全件を L2 で裁定)
    c_l2_direct = len(uniq_l1) * PRICE_L2
    print(f"  L2直結 (escalated {len(uniq_l1)} 件すべて L2):  ${c_l2_direct:.2f}")


if __name__ == "__main__":
    main()

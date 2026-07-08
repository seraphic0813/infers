"""ライブジャーナルの EVAL イベントを集計し、エントリー未発生時の「沈黙の中身」を
可視化する(改善: gate=none 手法の per-bar 可観測性)。

`--journal-eval` 付きで稼働した atr_trend_scalp のジャーナルには、毎確定足に1件の
EVAL イベントが残る。各 EVAL の reason は provider がそのバーで「どのゲートで見送りに
なったか」を示す(プランを出したバーは "signal")。本ツールはその内訳(ファネル)を
集計し、「バーは処理されているのにエントリーが出ない」状態が

  - 正常な低頻度(トレンド不成立=no_direction が大半)なのか、
  - 特定フィルタで恒常的に弾かれている(例: volume_filter が 100%)異常なのか

を切り分ける材料にする。取引判断には一切関与しない読み取り専用の集計。

使い方:
    python tools/eval_funnel.py work/journal/atr_trend_scalp_xauusd.jsonl
    python tools/eval_funnel.py <journal> --since 2026-07-06T13:20:00+00:00
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime

# provider の on_candle 内で理由が設定される順(=ファネルの深さ順)。表示順に使う。
REASON_ORDER = [
    "warmup",           # 指標ウォームアップ未完
    "position_open",    # ミラー上で建玉中(新規抑制)
    "out_of_session",   # セッションフィルタ時間外
    "no_direction",     # 上位足バイアス×EMAクロスの方向不成立(最も浅い実質ゲート)
    "no_retrace",       # EMA21 への押し目/戻り + 回復が未成立
    "atr_vol_filter",   # ATR が直近平均ATR比で不足
    "atr_floor",        # 相対ボラフロア(price比bps)未達
    "volume_filter",    # 出来高が SMA20 比で不足
    "spike_vol",        # 出来高スパイク上限超過(既定OFF)
    "spike_range",      # レンジスパイク上限超過(既定OFF)
    "atr_degenerate",   # ATR極小でTP1/TP2距離が縮退
    "signal",           # ★ プランを生成(=エントリー候補が出たバー)
    "unknown",          # reject_reason 未設定(理由注釈の無い出力)
]

REASON_LABEL = {
    "warmup": "ウォームアップ未完",
    "position_open": "建玉中(新規抑制)",
    "out_of_session": "セッション時間外",
    "no_direction": "方向不成立(トレンド外)",
    "no_retrace": "押し目/戻り未成立",
    "atr_vol_filter": "ATRボラ不足",
    "atr_floor": "相対ボラフロア未達",
    "volume_filter": "出来高不足",
    "spike_vol": "出来高スパイク除外",
    "spike_range": "レンジスパイク除外",
    "atr_degenerate": "ATR極小(縮退)",
    "signal": "★ シグナル生成",
    "unknown": "理由未設定",
}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("journal", help="ライブジャーナル(JSONL)のパス")
    ap.add_argument("--since", metavar="ISO8601",
                    help="この時刻(wall_ts)以降のEVALのみ集計(例: 直近の再起動時刻)")
    args = ap.parse_args()

    since = datetime.fromisoformat(args.since) if args.since else None

    counts: Counter[str] = Counter()
    total = 0
    first_ts: str | None = None
    last_ts: str | None = None
    signal_bars: list[str] = []
    try:
        with open(args.journal, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if row.get("kind") != "EVAL":
                    continue
                ts = row.get("wall_ts")
                if since is not None and ts is not None:
                    if datetime.fromisoformat(ts) < since:
                        continue
                reason = (row.get("data") or {}).get("reason", "unknown")
                counts[reason] += 1
                total += 1
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
                if reason == "signal":
                    signal_bars.append(row.get("bar_time") or ts)
    except FileNotFoundError:
        print(f"[error] ジャーナルが見つかりません: {args.journal}", file=sys.stderr)
        return 2

    if total == 0:
        print("EVAL イベントがありません。--journal-eval を付けて稼働したか、"
              "--since が新しすぎないか確認してください。", file=sys.stderr)
        return 1

    print(f"EVAL 集計: {total} バー  ({first_ts} 〜 {last_ts})")
    if since is not None:
        print(f"  (--since {args.since} 以降)")
    print()
    print(f"{'ゲート(見送り理由)':<24} {'件数':>7} {'割合':>8}")
    print("-" * 44)
    seen = set()
    for reason in REASON_ORDER:
        if reason in counts:
            seen.add(reason)
            n = counts[reason]
            label = REASON_LABEL.get(reason, reason)
            print(f"{label:<24} {n:>7} {100 * n / total:>7.2f}%")
    # 想定外(REASON_ORDER に無い)理由も末尾に出す
    for reason, n in counts.items():
        if reason not in seen:
            print(f"{reason:<24} {n:>7} {100 * n / total:>7.2f}%  (未分類)")

    sig = counts.get("signal", 0)
    print()
    print(f"シグナル生成バー: {sig} 件"
          + (f"  最新: {signal_bars[-1]}" if signal_bars else ""))
    if sig == 0:
        print("→ この期間はプランが1件も生成されていません(=エントリー候補ゼロ)。")
        # 最も深く到達したゲートを示す(no_direction が大半なら正常な低頻度)
        deepest = None
        for reason in reversed(REASON_ORDER):
            if reason in ("signal", "unknown"):
                continue
            if counts.get(reason):
                deepest = reason
                break
        if deepest:
            print(f"   最も深く到達したゲート: {REASON_LABEL.get(deepest, deepest)} "
                  f"({counts[deepest]} 件)。ここが実質の律速。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

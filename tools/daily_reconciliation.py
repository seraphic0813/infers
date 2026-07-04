"""日次リコンサイル・レポート(改善プランT13 OPS-3)。

ジャーナル(FSMイベント)とMT5の実約定履歴(deal history)を突合し、退出件数の
乖離を検出する日次レポート。**表示のみ・自動修復はしない**
(`execution/mt5_adapter.py` の `LiveRunner.reconcile()` は稼働中の状態機械を
実態へ合わせる別機能。本ツールは事後の「答え合わせ」であり、稼働ロジックには
一切触れない)。

smc_bos のファントム建玉事故(内部状態と実口座の乖離が長期間気づかれなかった
既知の事故クラス)のような問題を、日次の定期チェックで早期検知するための
監視ツール。

**限界**: ジャーナルの `bar_time`(確定足の判定時刻)と MT5 の実約定時刻には
数秒〜数分のずれがあり得るため、日付境界(UTC深夜)付近の取引では
まれに日ズレが生じ得る(監視の参考値であり、安全機構ではない)。

トレード判定コードには一切触れない。

使い方:
    python tools/daily_reconciliation.py work/journal/atr_trend_scalp_xauusd.jsonl \\
      --terminal-path "C:\\Program Files\\Vantage Trading MT5 Terminal3\\terminal64.exe" \\
      --symbol XAUUSD --day 2026-07-04
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from infers.journal import read_journal

_EXIT_TRANSITIONS = frozenset({"SL_HIT", "TP_CLOSE", "CLOSE_ALL"})


def count_journal_exits_for_day(journal_path: str | Path, day: dt.date) -> dict[str, int]:
    """指定UTC日の退出系FSMイベント(SL_HIT/TP_CLOSE/CLOSE_ALL)を種別ごとに数える。"""
    counts: dict[str, int] = {}
    for ev in read_journal(journal_path):
        if ev.kind != "FSM":
            continue
        transition = ev.data.get("transition")
        if transition not in _EXIT_TRANSITIONS:
            continue
        if not ev.bar_time:
            continue
        ev_day = dt.datetime.fromisoformat(ev.bar_time).date()
        if ev_day != day:
            continue
        counts[transition] = counts.get(transition, 0) + 1
    return counts


def count_mt5_exit_deals_for_day(terminal_path: str | None, *, symbol: str,
                                 day: dt.date) -> dict:
    """MT5の実約定履歴からその日の退出(DEAL_ENTRY_OUT)件数を取得する(読み取り専用)。"""
    try:
        import MetaTrader5 as mt5  # Windows専用・遅延 import
    except ImportError:
        return {"available": False, "error": "MetaTrader5 package not available"}

    init_kwargs = {"path": terminal_path} if terminal_path else {}
    try:
        if not mt5.initialize(**init_kwargs):
            return {"available": False, "error": str(mt5.last_error())}
        start = dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc)
        end = dt.datetime.combine(day, dt.time.max, tzinfo=dt.timezone.utc)
        deals = mt5.history_deals_get(start, end) or []
        exits = sum(1 for d in deals if d.symbol == symbol and d.entry == mt5.DEAL_ENTRY_OUT)
        return {"available": True, "exit_deals": exits}
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass


def compare(journal_counts: dict[str, int], mt5_exit_deals: int) -> dict:
    """ジャーナル側の退出イベント合計とMT5の実約定退出件数を突合する(純粋関数)。"""
    journal_total = sum(journal_counts.values())
    diff = journal_total - mt5_exit_deals
    return {
        "journal_exit_events": journal_counts,
        "journal_exit_total": journal_total,
        "mt5_exit_deals": mt5_exit_deals,
        "diff": diff,
        "match": diff == 0,
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("journal", type=Path, help="ライブジャーナル(JSONL)のパス")
    parser.add_argument("--terminal-path", metavar="PATH",
                        help="接続先MT5ターミナルのterminal64.exeパス(省略時はOS既定接続)")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--day", metavar="YYYY-MM-DD",
                        help="対象日(UTC。省略時は本日UTC)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not args.journal.exists():
        print(f"error: journal not found: {args.journal}", file=sys.stderr)
        return 2

    day = (dt.date.fromisoformat(args.day) if args.day
          else dt.datetime.now(dt.timezone.utc).date())

    journal_counts = count_journal_exits_for_day(args.journal, day)
    mt5_result = count_mt5_exit_deals_for_day(args.terminal_path, symbol=args.symbol, day=day)

    if args.json:
        import json
        payload = {"day": day.isoformat(), "journal_counts": journal_counts,
                  "mt5": mt5_result}
        if mt5_result.get("available"):
            payload["comparison"] = compare(journal_counts, mt5_result["exit_deals"])
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"日次リコンサイル・レポート: {args.journal.name} / {args.symbol} / {day.isoformat()}")
    print(f"ジャーナル退出イベント: {journal_counts} (合計 {sum(journal_counts.values())})")
    if not mt5_result.get("available"):
        print(f"MT5接続不可: {mt5_result.get('error')}")
        print("(MT5実約定との突合はスキップ。ジャーナル側の集計のみ表示)")
        return 1
    result = compare(journal_counts, mt5_result["exit_deals"])
    print(f"MT5実約定退出件数: {result['mt5_exit_deals']}")
    if result["match"]:
        print("判定: 一致(OK)")
        return 0
    print(f"判定: 不一致(diff={result['diff']}) — 原因調査を推奨"
          f"(自動修復はしない。手動でreconcile()状態またはMT5履歴を確認)")
    return 1


if __name__ == "__main__":
    sys.exit(main())

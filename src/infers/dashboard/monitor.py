"""ジャーナル監視 — 既存 journal.replay/read_journal の薄いラッパ。

ダッシュボードの監視パネル向けに、当日ジャーナルを JSON 化して返す。
ロジックは既存 `infers.journal` に委譲し、ここでは整形のみ行う
(トレード根幹・既存コードには一切触れない)。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from infers.journal import read_journal, replay


def journal_path_for(symbol: str, *, day: datetime | None = None) -> Path:
    """当日ジャーナルの既定パス (CLI run_live と同一規則)。"""
    stamp = (day or datetime.now(timezone.utc)).strftime("%Y%m%d")
    return Path("work/journal") / f"{symbol}_{stamp}.jsonl"


def list_journal_files(directory: str | Path = "work/journal") -> list[Path]:
    """ジャーナルディレクトリ内の *.jsonl を新しい順に列挙する。

    複数手法(複数CLIプロセス)が並行稼働しジャーナルパスをそれぞれ明示する運用
    (例: narrow_focus_xauusd.jsonl / smc_bos_xauusd.jsonl)を、手法名を一切
    ハードコードせず横並びで読み取り専用表示するための列挙ヘルパ。
    """
    d = Path(directory)
    if not d.is_dir():
        return []
    return sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def count_bars(path: str | Path) -> int:
    """一意な bar_time の本数 (進捗の軽量代理。status の高頻度 polling 用)。"""
    p = Path(path)
    if not p.exists():
        return 0
    return len({ev.bar_time for ev in read_journal(p) if ev.bar_time})


def _position_timelines(events) -> dict[str, list[str]]:
    """FSM イベントを position_id ごとの遷移タイムラインに畳み込む。"""
    timelines: dict[str, list[str]] = {}
    for ev in events:
        if ev.kind != "FSM":
            continue
        pid = str(ev.data.get("position_id", "?"))
        transition = str(ev.data.get("transition", "?"))
        timelines.setdefault(pid, []).append(transition)
    return timelines


def summarize(path: str | Path, *, from_ts: datetime | None = None,
              recent: int = 50) -> dict:
    """1 つのジャーナルファイルを監視パネル向け dict に要約する。

    存在しない場合は空サマリ (exists=False) を返す (起動直後・未稼働時)。
    """
    p = Path(path)
    if not p.exists():
        return {
            "exists": False,
            "path": str(p),
            "counts": {},
            "ai_client": None,
            "bars": 0,
            "positions": {},
            "recent_events": [],
            "golden": {"checked": 0, "ok": True, "mismatches": []},
        }

    events = list(read_journal(p))
    # bars: 一意な bar_time の本数 (進捗の代理。LiveRunner は無改変)
    bars = len({ev.bar_time for ev in events if ev.bar_time})

    result = replay(p, from_ts=from_ts)

    recent_events = [
        {
            "seq": ev.seq,
            "wall_ts": ev.wall_ts,
            "bar_time": ev.bar_time,
            "kind": ev.kind,
            "data": ev.data,
        }
        for ev in events[-recent:]
    ]

    return {
        "exists": True,
        "path": str(p),
        "counts": result.counts,
        "ai_client": result.ai_client,
        "bars": bars,
        "positions": _position_timelines(events),
        "recent_events": recent_events,
        "golden": {
            "checked": result.checked,
            "ok": result.ok,
            "mismatches": [list(m) for m in result.mismatches],
        },
    }

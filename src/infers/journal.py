"""イベントソーシング・ジャーナル (CLAUDE.md 第11条)。

全判断を特徴量スナップショットとともに **追記専用** (append-only) の JSONL へ
記録する。「ログに出してない判断」を作らないための恒久記録であり、ライブ稼働中の
クラッシュでも直前の1行しか失わない (各行で flush)。

  python -m infers.journal replay --file <path> [--from <ts>]

リプレイは2つの役割を持つ:
  1. 記録された判断タイムラインの要約・検証 (人間の事後点検)
  2. **ゴールデンリプレイ (回帰検証)**: ルールゲート ($0・決定論) で記録された
     セッションについて、保存済みの特徴量スナップショットを現在の
     `rule_judge.judge_features` に再投入し、過去と同一の判定 (decision) が
     再現されることを確認する。rule_judge.py の変更が過去判断を変えてしまう
     退行を検出する (同一入力→同一判断: CLAUDE.md 主要コマンド)。

設計原則:
  - 1行 = 1イベント。行は {seq, wall_ts, bar_time, kind, data}。
  - 判断内容は (kind, data, bar_time)。seq / wall_ts は監査用メタデータであり
    決定論比較には用いない (壁時計はリプレイで無視する)。
  - 価格はすべて整数ティック (CLAUDE.md 第6条)。Decimal は str 化して格納する。
  - 時刻は tz-aware UTC (第7条)。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

from infers.core.models import utc_now

# ルールゲートが特徴量を実評価した Verdict の source 値 (ゴールデンリプレイ対象)。
# POLICY (閾値未満) / GUARDRAIL (障害) は特徴量評価ではないため再計算対象外。
_FEATURE_EVALUATED_SOURCES = frozenset({"L1", "L2", "CACHE"})


# ---------------------------------------------------------------------------
# 書き込み (I/O アダプタ)
# ---------------------------------------------------------------------------

class JournalWriter:
    """追記専用 JSONL シンク。戦略コアからは注入された純粋なコールバックに見える。

    `core.loop.JournalSink` プロトコルを構造的に満たす。バックテストでは注入せず
    (None)、ライブ稼働でのみ配線する (大量の確定足でのファイルI/Oを避ける)。
    """

    def __init__(self, path: str | Path, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a", encoding="utf-8")
        self._seq = 0
        self._clock = clock
        self._cur_bar: datetime | None = None

    @property
    def path(self) -> Path:
        return self._path

    def set_bar(self, bar_time: datetime) -> None:
        """以降のイベントを紐づける確定足の時刻を更新する (決定論アンカー)。"""
        self._cur_bar = bar_time

    def record(self, kind: str, data: dict) -> None:
        """1イベントを1行追記して即 flush する (クラッシュ時の損失を1行に限定)。"""
        self._seq += 1
        line = {
            "seq": self._seq,
            "wall_ts": self._clock().isoformat(),
            "bar_time": self._cur_bar.isoformat() if self._cur_bar else None,
            "kind": kind,
            "data": data,
        }
        self._fh.write(json.dumps(line, default=str, ensure_ascii=False) + "\n")
        self._fh.flush()

    def fsm_sink(self, position_id: str) -> Callable[[str, dict], None]:
        """PositionFSM へ注入する遷移コールバックを返す (全状態遷移を FSM イベント化)。"""
        def sink(transition: str, payload: dict) -> None:
            self.record("FSM", {"position_id": position_id,
                                 "transition": transition, **payload})
        return sink

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "JournalWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# 読み込み
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class JournalEvent:
    seq: int
    wall_ts: str
    bar_time: str | None
    kind: str
    data: dict


def read_journal(path: str | Path) -> Iterator[JournalEvent]:
    """追記順のままイベントを読み出す (空行は無視)。"""
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            yield JournalEvent(
                seq=int(obj["seq"]),
                wall_ts=str(obj.get("wall_ts", "")),
                bar_time=obj.get("bar_time"),
                kind=str(obj["kind"]),
                data=dict(obj.get("data", {})),
            )


# ---------------------------------------------------------------------------
# リプレイ (要約 + ゴールデン回帰検証)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReplayResult:
    total_events: int
    counts: dict[str, int]
    ai_client: str | None
    checked: int                              # 再計算したルールVerdict数
    mismatches: tuple[tuple[int, str, str], ...]   # (seq, recorded_decision, recomputed_decision)

    @property
    def ok(self) -> bool:
        return not self.mismatches


def _bar_dt(ev: JournalEvent) -> datetime | None:
    if not ev.bar_time:
        return None
    return datetime.fromisoformat(ev.bar_time)


def replay(path: str | Path, *, from_ts: datetime | None = None) -> ReplayResult:
    """ジャーナルを読み、要約とゴールデン回帰検証を行う。

    ai_client=="rule" のセッションに限り、特徴量を評価した Verdict
    (source ∈ {L1,L2,CACHE}) を現在の `judge_features` へ再投入し、
    記録済み decision と一致するかを検証する。
    """
    events = list(read_journal(path))
    if from_ts is not None:
        events = [e for e in events
                  if (_bar_dt(e) is None or _bar_dt(e) >= from_ts)]

    counts = Counter(e.kind for e in events)
    session = next((e for e in events if e.kind == "SESSION"), None)
    ai_client = session.data.get("ai_client") if session else None

    checked = 0
    mismatches: list[tuple[int, str, str]] = []
    if ai_client == "rule":
        from infers.ai.rule_judge import judge_features  # 遅延 import (replay時のみ)
        for e in events:
            if e.kind != "VERDICT":
                continue
            if e.data.get("source") not in _FEATURE_EVALUATED_SOURCES:
                continue
            features = e.data.get("features")
            if not features:
                continue
            recomputed = judge_features(int(e.data["direction"]), features)
            checked += 1
            recorded = str(e.data.get("decision"))
            if recomputed.decision != recorded:
                mismatches.append((e.seq, recorded, recomputed.decision))

    return ReplayResult(
        total_events=len(events),
        counts=dict(counts),
        ai_client=ai_client,
        checked=checked,
        mismatches=tuple(mismatches),
    )


def _format_position_timelines(path: str | Path,
                               from_ts: datetime | None) -> list[str]:
    """ポジション別の FSM 遷移タイムラインを行リストに整形する (人間点検用)。"""
    timelines: dict[str, list[str]] = {}
    for e in read_journal(path):
        if e.kind != "FSM":
            continue
        if from_ts is not None and _bar_dt(e) is not None and _bar_dt(e) < from_ts:
            continue
        pid = str(e.data.get("position_id", "?"))
        timelines.setdefault(pid, []).append(str(e.data.get("transition", "?")))
    lines: list[str] = []
    for pid, transitions in timelines.items():
        lines.append(f"  {pid}: " + " -> ".join(transitions))
    return lines


def _cmd_replay(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"error: journal not found: {path}", file=sys.stderr)
        return 2
    from_ts = datetime.fromisoformat(args.from_ts) if args.from_ts else None

    result = replay(path, from_ts=from_ts)
    print(f"journal: {path}")
    print(f"events: {result.total_events}"
          + (f" (from {from_ts.isoformat()})" if from_ts else ""))
    print("kinds: " + "  ".join(f"{k}={v}" for k, v in sorted(result.counts.items())))
    print(f"ai_client: {result.ai_client}")

    timelines = _format_position_timelines(path, from_ts)
    if timelines:
        print("positions:")
        for line in timelines:
            print(line)

    if result.ai_client != "rule":
        print("golden-replay: skipped (ルールゲート以外は決定論再計算の対象外)")
        return 0

    print(f"golden-replay: {result.checked} 件のルールVerdictを再計算")
    if result.ok:
        print("  OK - 全件で記録判断と再計算判断が一致 (回帰なし)")
        return 0
    print(f"  MISMATCH - {len(result.mismatches)} 件が不一致 (rule_judge の退行):",
          file=sys.stderr)
    for seq, recorded, recomputed in result.mismatches:
        print(f"    seq={seq}: recorded={recorded} != recomputed={recomputed}",
              file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    # Windows のコンソール (cp932 等) でも日本語・記号で落ちないよう UTF-8 へ再設定。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")            # py3.7+
        except (AttributeError, ValueError):                # 既にラップ済み等
            pass
    p = argparse.ArgumentParser(prog="infers.journal",
                                description="イベントソーシング・ジャーナルのリプレイ")
    sub = p.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("replay", help="ジャーナルの要約 + ゴールデン回帰検証")
    rp.add_argument("--file", required=True, help="ジャーナル JSONL のパス")
    rp.add_argument("--from", dest="from_ts", metavar="ISO_TS",
                    help="この確定足時刻(UTC ISO)以降のイベントに限定")
    args = p.parse_args(argv)
    if args.cmd == "replay":
        return _cmd_replay(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

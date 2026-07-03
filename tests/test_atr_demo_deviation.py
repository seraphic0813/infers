"""tools/atr_demo_deviation.py のテスト(改善プランT8)。

JournalWriter で実際のジャーナルスキーマ通りの FSM イベント列を書き出し、
乖離集計ロジック(トレール/初期SLの分類・符号規約)を検証する。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from infers.journal import JournalWriter

from atr_demo_deviation import compute_deviations, summarize  # noqa: E402


def _write_fsm(journal: JournalWriter, position_id: str, transition: str, **payload):
    journal.record("FSM", {"position_id": position_id, "transition": transition, **payload})


def test_initial_stop_deviation_long(tmp_path):
    """OPEN状態(TP1未到達)でのSL_HITは initial_stop に分類される。"""
    path = tmp_path / "j.jsonl"
    with JournalWriter(path) as j:
        _write_fsm(j, "p1", "MARKET_ENTRY", fill=1000, sl=990, tp1=1010, tp2=1030,
                  trail=5, volume=2, half=1, state="OPEN")
        _write_fsm(j, "p1", "SL_HIT", price=988, state="CLOSED")

    devs = compute_deviations(path)
    assert len(devs) == 1
    d = devs[0]
    assert d.category == "initial_stop"
    assert d.expected_sl == 990
    assert d.actual_fill == 988
    assert d.deviation_ticks == 2   # 買い: 期待990より2tick不利(安く)ヒット


def test_trail_deviation_long(tmp_path):
    """TP_CLOSE(tp1)後のRUNNER状態でのSL_HITはtrailに分類され、直近MOVE_SLを期待値に使う。"""
    path = tmp_path / "j.jsonl"
    with JournalWriter(path) as j:
        _write_fsm(j, "p1", "MARKET_ENTRY", fill=1000, sl=990, tp1=1010, tp2=1030,
                  trail=5, volume=2, half=1, state="OPEN")
        _write_fsm(j, "p1", "TP_CLOSE", level="tp1", closed=1, runner=1, state="OPEN")
        _write_fsm(j, "p1", "MOVE_SL", new_sl=1000, state="RUNNER")   # 建値化
        _write_fsm(j, "p1", "MOVE_SL", new_sl=1010, state="RUNNER")   # トレール前進
        _write_fsm(j, "p1", "SL_HIT", price=1007, state="CLOSED")

    devs = compute_deviations(path)
    assert len(devs) == 1
    d = devs[0]
    assert d.category == "trail"
    assert d.expected_sl == 1010    # 直近のMOVE_SL
    assert d.actual_fill == 1007
    assert d.deviation_ticks == 3


def test_short_position_deviation(tmp_path):
    """売りポジション(fill<sl)は direction=-1 として符号が正しく解決される。"""
    path = tmp_path / "j.jsonl"
    with JournalWriter(path) as j:
        _write_fsm(j, "p1", "MARKET_ENTRY", fill=1000, sl=1010, tp1=990, tp2=970,
                  trail=5, volume=2, half=1, state="OPEN")
        _write_fsm(j, "p1", "TP_CLOSE", level="tp1", closed=1, runner=1, state="OPEN")
        _write_fsm(j, "p1", "MOVE_SL", new_sl=1000, state="RUNNER")
        _write_fsm(j, "p1", "MOVE_SL", new_sl=990, state="RUNNER")
        _write_fsm(j, "p1", "SL_HIT", price=993, state="CLOSED")

    devs = compute_deviations(path)
    assert len(devs) == 1
    d = devs[0]
    assert d.category == "trail"
    assert d.expected_sl == 990
    assert d.actual_fill == 993
    assert d.deviation_ticks == 3    # 売り: 期待990より高い(不利な)価格でヒット


def test_favorable_or_exact_fill_is_non_positive(tmp_path):
    """期待SLちょうどでの約定 (SimBroker理想) は乖離0。"""
    path = tmp_path / "j.jsonl"
    with JournalWriter(path) as j:
        _write_fsm(j, "p1", "MARKET_ENTRY", fill=1000, sl=990, tp1=1010, tp2=1030,
                  trail=5, volume=2, half=1, state="OPEN")
        _write_fsm(j, "p1", "SL_HIT", price=990, state="CLOSED")

    devs = compute_deviations(path)
    assert devs[0].deviation_ticks == 0


def test_incomplete_journal_sl_hit_without_entry_is_skipped(tmp_path):
    """MARKET_ENTRYの記録より前から始まるジャーナル片(途中からの記録)は静かにスキップ。"""
    path = tmp_path / "j.jsonl"
    with JournalWriter(path) as j:
        _write_fsm(j, "orphan", "SL_HIT", price=100, state="CLOSED")

    assert compute_deviations(path) == []


def test_multiple_positions_tracked_independently(tmp_path):
    path = tmp_path / "j.jsonl"
    with JournalWriter(path) as j:
        _write_fsm(j, "p1", "MARKET_ENTRY", fill=1000, sl=990, tp1=1010, tp2=1030,
                  trail=5, volume=2, half=1, state="OPEN")
        _write_fsm(j, "p2", "MARKET_ENTRY", fill=2000, sl=2010, tp1=1990, tp2=1970,
                  trail=5, volume=2, half=1, state="OPEN")
        _write_fsm(j, "p1", "SL_HIT", price=988, state="CLOSED")
        _write_fsm(j, "p2", "SL_HIT", price=2013, state="CLOSED")

    devs = compute_deviations(path)
    by_pid = {d.position_id: d for d in devs}
    assert by_pid["p1"].category == "initial_stop" and by_pid["p1"].deviation_ticks == 2
    assert by_pid["p2"].category == "initial_stop" and by_pid["p2"].deviation_ticks == 3


def test_summarize_median_and_p90():
    from atr_demo_deviation import Deviation
    devs = [Deviation(position_id=f"p{i}", category="trail", expected_sl=0,
                      actual_fill=0, deviation_ticks=v)
            for i, v in enumerate([1, 2, 3, 4, 5, 100])]
    stats = summarize(devs, "trail")
    assert stats["n"] == 6
    assert stats["median_ticks"] == 3.5
    assert stats["p90_ticks"] == 5           # idx=round(0.9*5)=4(銀行丸め)→sorted[4]=5
    assert stats["min_ticks"] == 1 and stats["max_ticks"] == 100


def test_summarize_empty_category_returns_zero_n():
    assert summarize([], "trail") == {"n": 0}

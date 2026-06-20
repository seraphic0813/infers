"""手法ごとのレポート表示記述子 (L0契約 / 設計書 §8補助)。

`backtest/report_html.py`(L4)は手法固有の語彙(ジャーナルイベント名・
features キー・凡例・根拠ライン)を一切知らず、本モジュールの
`StrategyReportSpec` を介して各手法(L2)から表示方法を受け取る。
`core/execution.py` の `ExecutionModel`/`BrokerPort` を L0 に置き L2 がそれを
実装する構図と同型(L2→L0 の依存方向を保ち、L4 は具象手法を import しない)。

`classify_exits` のみ Python 側専用(JSONへは出さない。`BacktestRecorder` が
退出種別をジャーナルから事前計算するために使う)。他のフィールドは
JSON化されて `report_data.js` の `BT.display` として埋め込まれ、JS側の
描画(凡例・オーバーレイ表示可否・根拠ライン・トレード表の根拠列等)を駆動する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

JournalEntry = tuple[str, dict]


def _default_classify_exits(journal: list[JournalEntry]) -> list[str]:
    return ["CLOSE" for name, _ in journal if name == "CLOSE_ALL"]


@dataclass(frozen=True)
class StrategyReportSpec:
    """1手法のレポート表示契約。

    None/空の項目は「この手法では非表示」を意味する(表示可否制御)。
    """

    name: str
    volume_label: str = "建玉"
    classify_exits: Callable[[list[JournalEntry]], list[str]] = _default_classify_exits
    exit_meta: dict[str, dict] = field(default_factory=dict)
    # (TradePlan辞書のキー, ラベル, 色, ラインスタイル["solid"|"dashed"|"dotted"]) —
    # 根拠ラインとして描画する項目のみ列挙する。
    plan_lines: tuple[tuple[str, str, str, str], ...] = ()
    # 汎用オーバーレイの表示可否(表示可否制御)。sma/ema は表示する期間のリスト。
    overlays: dict = field(default_factory=lambda: {"sma": (), "ema": (), "rsi": False})
    # (featuresキー, ラベル) — 詳細パネルの汎用フィールド一覧
    feature_fields: tuple[tuple[str, str], ...] = ()
    # トレード表の「根拠」列に使う features キー(Noneで列自体を非表示)
    family_column: str | None = None
    # 凡例エントリー (色, ラベル)。色が空文字なら色見本なしのプレーンテキスト行。
    legend: tuple[tuple[str, str], ...] = ()
    # 詳細パネルの拡張ブロック(能力フラグ。例: "dow_confluence_panel")。
    # 該当フラグを持つ手法のみ、その手法専用の詳細描画コードが発火する。
    extras: frozenset[str] = frozenset()


def generic_classify_exits(journal: list[JournalEntry]) -> list[str]:
    """成行参入+固定SL/TP系(market_tpsl・smc_bos等)に共通する退出分類。

    MOVE_SL が先行していれば、その後のSLヒットを「保護SL」として区別する
    (建値・トレール等、初期SLから改善された状態でのSLヒット)。
    """
    kinds: list[str] = []
    sl_advanced = False
    for name, payload in journal:
        if name == "MOVE_SL":
            sl_advanced = True
        elif name == "TP_CLOSE":
            kinds.append("TP")
        elif name == "SL_HIT":
            kinds.append("PROT_SL" if sl_advanced else "SL")
        elif name == "CLOSE_ALL":
            reason = payload.get("reason") if isinstance(payload, dict) else None
            kinds.append("EOD" if reason == "END_OF_DATA" else "CLOSE")
    return kinds


GENERIC_EXIT_META: dict[str, dict] = {
    "TP": {"label": "利確(TP)", "color": "#26a69a", "shape": "square"},
    "PROT_SL": {"label": "保護SL", "color": "#ffb74d", "shape": "circle"},
    "SL": {"label": "損切りSL", "color": "#ef5350", "shape": "circle"},
    "EOD": {"label": "期末手仕舞い", "color": "#9aa0ab", "shape": "square"},
    "CLOSE": {"label": "手仕舞い", "color": "#b2b5be", "shape": "square"},
}

GENERIC_REPORT_SPEC = StrategyReportSpec(
    name="generic",
    volume_label="建玉",
    classify_exits=generic_classify_exits,
    exit_meta=GENERIC_EXIT_META,
    plan_lines=(
        ("limit", "エントリー", "#26a69a", "dashed"),
        ("sl", "SL", "#ef5350", "solid"),
        ("fib_target", "TP", "#ab47bc", "dashed"),
    ),
    overlays={"sma": (), "ema": (), "rsi": False},
    feature_fields=(),
    family_column=None,
    legend=(
        ("#26a69a", "エントリー (▲買/▼売)"),
        ("#ab47bc", "利確TP (■)"),
        ("#ffb74d", "保護SL (●)"),
        ("#ef5350", "損切りSL (●)"),
        ("#ffd54f", "選択トレード (黄=ハイライト, 選択時は他トレード非表示)"),
    ),
    extras=frozenset(),
)

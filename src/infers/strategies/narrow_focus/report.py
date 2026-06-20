"""Narrow Focus 手法のレポート表示記述子 (L2 / core.report_spec.StrategyReportSpec)。

`classify_exits` は段階(R1以前)の `backtest/report_html.py` から物理移設した
ものであり、ロジックは1ビットも変更していない(depth50レポートの不変を担保)。
`infers.backtest.report_html` からは後方互換のため遅延 re-export される。
"""

from __future__ import annotations

from infers.core.report_spec import StrategyReportSpec

# 退出イベント → 表示種別。台帳 exits と同順で並ぶ (各退出が1件のジャーナル
# イベントに対応する)。"TP"=半分利確, "BE_SL"=建値SL(建値移動後のSL), "SL"=損切り,
# 残玉決済 CLOSE_ALL は reason で細分: "FIB"=フィボ目標到達, "DOW"=ダウ転換,
# "EOD"=データ末尾手仕舞い, "CLOSE"=その他。
_CLOSE_REASON_KIND = {
    "FIB_TARGET": "FIB",
    "DOW_REVERSAL": "DOW",
    "END_OF_DATA": "EOD",
}


def classify_exits(journal: list) -> list[str]:
    """FSMジャーナルを走査し、各退出の種別を発生順に返す。

    - 建値SL移動 (SL_TO_BREAKEVEN) 後の SL_HIT は損切りではなく「建値SL」。
    - 残玉決済 (CLOSE_ALL) は reason により フィボ目標 / ダウ転換 / 期末 を区別する
      (チャートで『なぜ閉じたか』を目視検証できるように)。
    """
    kinds: list[str] = []
    be_active = False
    for name, payload in journal:
        if name == "SL_TO_BREAKEVEN":
            be_active = True
        elif name == "HALF_TAKE_PROFIT":
            kinds.append("TP")
        elif name == "SL_HIT":
            kinds.append("BE_SL" if be_active else "SL")
        elif name == "CLOSE_ALL":
            reason = payload.get("reason") if isinstance(payload, dict) else None
            kinds.append(_CLOSE_REASON_KIND.get(reason, "CLOSE"))
    return kinds


EXIT_META: dict[str, dict] = {
    "TP": {"label": "半分利確", "color": "#26a69a", "shape": "square"},
    "BE_SL": {"label": "建値SL", "color": "#ffb74d", "shape": "circle"},
    "SL": {"label": "損切りSL", "color": "#ef5350", "shape": "circle"},
    "FIB": {"label": "フィボ目標利確", "color": "#ab47bc", "shape": "square"},
    "DOW": {"label": "ダウ転換決済", "color": "#42a5f5", "shape": "square"},
    "EOD": {"label": "期末手仕舞い", "color": "#9aa0ab", "shape": "square"},
    "CLOSE": {"label": "手仕舞い", "color": "#b2b5be", "shape": "square"},
}

NARROW_FOCUS_REPORT_SPEC = StrategyReportSpec(
    name="narrow_focus",
    volume_label="打診",
    classify_exits=classify_exits,
    exit_meta=EXIT_META,
    plan_lines=(
        ("limit", "指値(合流点)", "#26a69a", "dashed"),
        ("sl", "SL初期", "#ef5350", "solid"),
        ("invalidation", "エリオット無効化", "#ff6d00", "dashed"),
        ("w1_high", "第1波高値(追撃基準)", "#2962ff", "dashed"),
        ("fib_target", "フィボ161.8%目標", "#ab47bc", "dashed"),
    ),
    overlays={"sma": (90, 200), "ema": (), "rsi": True},
    feature_fields=(),   # 詳細パネルは dow_confluence_panel(extras)が一括描画
    family_column="families",
    legend=(
        ("#2962ff", "SMA90"),
        ("#f59e0b", "SMA200"),
        ("#26a69a", "エントリー (▲買/▼売) / 半分利確 (■)"),
        ("#ab47bc", "フィボ目標利確 (■)"),
        ("#42a5f5", "ダウ転換決済 (■)"),
        ("#ffb74d", "建値SL (●)"),
        ("#ef5350", "損切りSL (●)"),
        ("#26a69a", "指値発注 (◇) 〜 失効期限 (◇) ・有効期間=破線"),
        ("#ffd54f", "選択トレード (黄=ハイライト, 選択時は他トレード非表示)"),
        ("#d4af37", "FIB押し戻し (38.2/50/61.8/78.6)"),
        ("#4dd0e1", "SRゾーン (上下端)"),
        ("", "— 根拠ライン: 指値 / SL初期 / 無効化 / 第1波高値 / フィボ161.8%目標 / SMA90・200"),
    ),
    extras=frozenset({"dow_confluence_panel"}),
)

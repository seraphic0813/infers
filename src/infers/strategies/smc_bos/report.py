"""smc_bos 手法のレポート表示記述子 (L2 / core.report_spec.StrategyReportSpec)。

退出分類は market_tpsl と共通の `generic_classify_exits`(成行+固定SL/TP系の
ジャーナル語彙: MARKET_ENTRY/MOVE_SL/TP_CLOSE/SL_HIT/CLOSE_ALL)を再利用する。
SMC固有なのは凡例・根拠ライン・詳細パネルのfeatures一覧・チャートオーバーレイ
(EMA80。本手法の核心フィルターのため表示する)のみ。
"""

from __future__ import annotations

from infers.core.report_spec import GENERIC_EXIT_META, StrategyReportSpec, generic_classify_exits

SMC_BOS_REPORT_SPEC = StrategyReportSpec(
    name="smc_bos",
    volume_label="成行参入",
    classify_exits=generic_classify_exits,
    exit_meta=GENERIC_EXIT_META,
    plan_lines=(
        ("limit", "参入(成行)", "#26a69a", "dashed"),
        ("sl", "SL(初期)", "#ef5350", "solid"),
        ("fib_target", "TP(固定RR)", "#ab47bc", "dashed"),
    ),
    overlays={"sma": (), "ema": (80,), "rsi": False},
    feature_fields=(
        ("ema80", "EMA80"),
        ("atr", "ATR"),
        ("swing_high", "直近スイング高値"),
        ("swing_low", "直近スイング安値"),
    ),
    family_column=None,   # コンフルエンス概念を持たないため根拠列は非表示
    legend=(
        ("#7e57c2", "EMA80(コア・トレンドフィルター)"),
        ("#26a69a", "エントリー (▲買/▼売)"),
        ("#ab47bc", "TP(固定RR) (■)"),
        ("#ffb74d", "保護SL (●) ・ be_mode=at_1r/structure 時のみ発生"),
        ("#ef5350", "損切りSL (●)"),
        ("#ffd54f", "選択トレード (黄=ハイライト, 選択時は他トレード非表示)"),
        ("", "— 根拠ライン: 参入 / SL(初期) / TP(固定RR)"),
    ),
    extras=frozenset(),
)

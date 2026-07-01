"""atr_trend_scalp 手法のレポート表示記述子 (L2 / core.report_spec.StrategyReportSpec)。

退出分類は market_tpsl / smc_bos と共通の `generic_classify_exits`(成行+固定SL/TP系の
ジャーナル語彙: MARKET_ENTRY/MOVE_SL/TP_CLOSE/SL_HIT/CLOSE_ALL)を再利用する。
本手法は TP1(半利)・TP2(残玉)とも `TP_CLOSE` を記録し、建値化・トレールは
`MOVE_SL` を記録するため、MOVE_SL 先行後のSLヒットは「保護SL」として区別される。

固有なのは凡例・根拠ライン・詳細パネルの features 一覧・チャートオーバーレイ
(EMA9/21。本手法の核心フィルター)。上位足 EMA50(M15)は執行TF(M5)チャートへ
直接重ねられないため描画しない(詳細パネルの数値では表示する)。
"""

from __future__ import annotations

from infers.core.report_spec import GENERIC_EXIT_META, StrategyReportSpec, generic_classify_exits

ATR_TREND_SCALP_REPORT_SPEC = StrategyReportSpec(
    name="atr_trend_scalp",
    volume_label="成行参入",
    classify_exits=generic_classify_exits,
    exit_meta=GENERIC_EXIT_META,
    plan_lines=(
        ("limit", "参入(成行)", "#26a69a", "dashed"),
        ("sl", "SL(初期)", "#ef5350", "solid"),
        ("fib_target", "TP2(2.0×ATR)", "#ab47bc", "dashed"),
    ),
    overlays={"sma": (), "ema": (9, 21), "rsi": False},
    feature_fields=(
        ("ema_fast", "EMA9"),
        ("ema_medium", "EMA21"),
        ("ema_trend", "EMA50(M15)"),
        ("atr", "ATR14"),
        ("vol_sma", "出来高SMA20"),
    ),
    family_column=None,   # コンフルエンス概念を持たないため根拠列は非表示
    legend=(
        ("#26a69a", "EMA9 ・ エントリー (▲買/▼売)"),
        ("#ffa726", "EMA21(押し目基準)"),
        ("#ab47bc", "TP2(2.0×ATR) (■)"),
        ("#ffb74d", "保護SL (●) ・ 建値化/トレール後のSLヒット"),
        ("#ef5350", "損切りSL (●)"),
        ("#ffd54f", "選択トレード (黄=ハイライト, 選択時は他トレード非表示)"),
        ("", "— 根拠ライン: 参入 / SL(初期) / TP2。TP1(1.0×ATR)で半利+建値化 → 0.5×ATRトレール"),
    ),
    extras=frozenset(),
)

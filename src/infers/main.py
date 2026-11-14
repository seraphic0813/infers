"""INFERS 統合エントリーポイント (フェーズ8〜10)。

  python -m infers.main --mode backtest --data data/xauusd_m5.parquet
  python -m infers.main --mode replay   --data data/xauusd_m5.parquet   # backtest同義
  python -m infers.main --mode live --demo
  python -m infers.main --mode export --data out.parquet --years 5

AI層 (CLAUDE.md A-12): トレード判定にLLMは現在不使用(正式方針)。ゲートは
決定論のみ — narrow_focus/depth50 は `ai/rule_judge.py`($0・ルールベース。
verdict_cache 併用)、他手法は `--ai-client none`(常時GOのパススルー)。
LLM実呼び出し系(Batches API・2パス収集モード)は撤去済み(改善プラン T6)。

安全ガード:
  - live モードは --demo が既定。実口座は --allow-real-account の明示が必須
  - SignalProvider は "module:factory" 形式で注入する。既定は
    InfersSignalProvider。NullProvider (監視のみ・発注ゼロ) へ差し替え可能
  - APIキー・口座資格情報は環境変数のみ
"""

from __future__ import annotations

import argparse
import importlib
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from infers.ai.gateway import PROMPT_VERSION, AiGateway, EscalationPolicy, VerdictCache
from infers.ai.rule_judge import RuleBasedLlmClient
from infers.core.loop import ProviderOutput, SignalProvider
from infers.core.models import Candle, SymbolSpec, Timeframe
from infers.execution.risk import RiskConfig, RiskManager
from infers.execution.sm import FsmConfig

SYMBOLS: dict[str, SymbolSpec] = {
    "XAUUSD": SymbolSpec(name="XAUUSD", tick_size=Decimal("0.01"),
                         lot_step=Decimal("0.01"), digits=2),
    "BTCUSD": SymbolSpec(name="BTCUSD", tick_size=Decimal("0.01"),
                         lot_step=Decimal("0.01"), digits=2),
}

# ルールベースゲートは $0 のため L2 予算キャップが不要 (キャップは旧Claude課金対策)。
# キャップを残すと L2_AFTER_L1 案件が日次4件目以降 GUARDRAIL NO_GO に化ける。
RULE_POLICY = EscalationPolicy(score_l1=Decimal(2), score_l2=Decimal(4),
                               ambiguity_gray=Decimal("0.1"),
                               l2_daily_call_cap=1_000_000)
DEFAULT_RISK = RiskConfig(max_position_volume_steps=4, max_total_volume_steps=8,
                          max_spread_ticks=50, daily_loss_limit_tick_steps=50_000)
DEFAULT_FSM = FsmConfig(min_be_distance_ticks=10, be_offset_ticks=2,
                        breakout_buffer_ticks=10)


class NullProvider:
    """監視のみ (プラン発行ゼロ)。--provider で明示指定して使う安全弁。"""

    def on_candle(self, candle: Candle) -> ProviderOutput:
        return ProviderOutput()


def load_provider(spec_str: str | None) -> SignalProvider:
    """"package.module:factory" を import してプロバイダを生成する。"""
    if not spec_str:
        return NullProvider()
    module_name, _, attr = spec_str.partition(":")
    if not module_name or not attr:
        raise ValueError(f"provider must be 'module:factory', got {spec_str!r}")
    factory = getattr(importlib.import_module(module_name), attr)
    return factory()


def build_provider(args: argparse.Namespace) -> SignalProvider:
    """既定は InfersSignalProvider (フェーズ9: 分析層フルパイプライン)。

    --provider 'module:factory' で差し替え可能 (NullProvider 等)。
    """
    if args.provider:
        return load_provider(args.provider)
    if args.strategy:
        from infers.strategies.registry import get_strategy
        return get_strategy(args.strategy).build(symbol=args.symbol, tf=Timeframe(args.tf))
    from infers.strategies.narrow_focus.provider import InfersSignalProvider, ProviderConfig
    rsi_mtfs = (() if args.no_rsi_mtf
                else tuple(Timeframe(t) for t in args.rsi_mtf))
    macro_k = (Decimal(str(args.macro_k_atr_reversal))
               if args.macro_k_atr_reversal is not None else None)
    micro_k = (Decimal(str(args.micro_k_atr_reversal))
               if args.micro_k_atr_reversal is not None else None)
    depth_kw = ({"depth_max": Decimal(str(args.depth_max))}
                if args.depth_max is not None else {})
    if args.depth_tier:
        depth_kw["depth_tier"] = True
    if args.macro_adaptive_depth:
        depth_kw["macro_adaptive_depth"] = True
    if args.depth_max_shallow is not None:
        depth_kw["depth_max_shallow"] = Decimal(str(args.depth_max_shallow))
    if args.shallow_min_families is not None:
        depth_kw["shallow_min_families"] = args.shallow_min_families
    if args.expiry_recovery:
        depth_kw["expiry_recovery"] = True
    cfg = ProviderConfig(macro_filter=not args.no_macro_filter,
                         macro_tf=Timeframe(args.macro_tf),
                         wave2_tf=Timeframe(args.wave2_tf),
                         rsi_macro_tfs=rsi_mtfs,
                         score_fib=not args.no_fib_score,
                         depth_screen=args.depth_screen,
                         macro_wave2=args.macro_wave2,
                         be_sl_macro_tf=args.be_sl_macro_tf,
                         macro_k_atr_reversal=macro_k,
                         micro_k_atr_reversal=micro_k,
                         **depth_kw)
    return InfersSignalProvider(symbol=args.symbol, tf=Timeframe(args.tf), config=cfg)


def build_execution_factory(args: argparse.Namespace):
    """手法レジストリが指定する執行モデル生成器 (段階2.5)。

    --strategy 指定時はそのレジストリ登録の build_execution を返す(market_tpsl 等は
    別執行モデルを持つ)。未指定・--provider 経路・build_execution=None の手法では
    None を返し、TradingLoop は既定の Narrow Focus 執行を使う(従来挙動・1ビット不変)。
    """
    if args.strategy and not args.provider:
        from infers.strategies.registry import get_strategy
        return get_strategy(args.strategy).build_execution
    return None


def resolve_report_spec(args: argparse.Namespace):
    """HTMLレポートの表示記述子 (core.report_spec.StrategyReportSpec) を解決する。

    --strategy 指定時はレジストリ登録の report_spec(未登録なら None →
    GENERIC_REPORT_SPEC へフォールバック)。--provider 指定時は何が来るか
    分からないため汎用フォールバック。いずれも未指定 (従来のフラグ組み立て
    経路 = 常に Narrow Focus) では NARROW_FOCUS_REPORT_SPEC を返す
    (depth50 等の既存レポートを1ビットも変えないため)。
    """
    if args.provider:
        return None
    if args.strategy:
        from infers.strategies.registry import get_strategy
        return get_strategy(args.strategy).report_spec
    from infers.strategies.narrow_focus.report import NARROW_FOCUS_REPORT_SPEC
    return NARROW_FOCUS_REPORT_SPEC


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="infers", description="INFERS trading system")
    p.add_argument("--mode", choices=["backtest", "replay", "live", "export"],
                   required=True)
    p.add_argument("--symbol", choices=sorted(SYMBOLS), default="XAUUSD")
    p.add_argument("--tf", choices=[t.value for t in Timeframe], default="M5")
    p.add_argument("--data", help="Parquetパス (backtest: 入力 / export: 出力)")
    p.add_argument("--provider",
                   help="SignalProvider factory ('module:attr')。"
                        "省略時は InfersSignalProvider (フルパイプライン)")
    p.add_argument("--strategy",
                   help="手法レジストリ(infers.strategies.registry)から名前で選択 "
                        "(例: 'depth50' = v1.0確定ベースライン)。指定時は他の手法系"
                        "フラグ(--depth-max 等)を無視し、レジストリ登録済みの構成を"
                        "そのまま使う。省略時は従来のフラグ組み立て経路(挙動不変)")
    p.add_argument("--verdict-cache", default="work/cache/verdicts.sqlite3")
    p.add_argument("--ai-client", choices=["rule", "none"], default=None,
                   help="エントリーゲートの判定方式。省略時は解決順序で決まる"
                        "(resolve_ai_client: ①本フラグ明示 → ②--strategy のレジストリ"
                        "宣言(StrategySpec.gate) → ③レガシー既定 'rule')。"
                        "rule は rule_judge.py の決定論ルール ($0, narrow_focus_v3.md "
                        "§5 実装)。none は手法非依存のパススルー(常時GO。Narrow Focus "
                        "固有特徴量を前提としない別手法 — market_tpsl/smc_bos 等 — の"
                        "バックテスト用。防御層は不変。spec.md §5.7 案A)。トレード判定に"
                        "LLMは現在不使用のため LLM経由のゲートは選択肢に無い"
                        "(CLAUDE.md A-12)")
    p.add_argument("--years", type=int, default=5, help="export: 取得年数")
    p.add_argument("--max-bars", type=int, help="live: 処理バー数上限 (検証用)")
    p.add_argument("--warm-bars", type=int, default=15000,
                   help="live: 起動時に歴史バーをプロバイダーへ食わせてウォームアップする本数 "
                        "(D1 ZigZag+Dow確立には約10本のD1スイングが必要: "
                        "M5換算で ~15000本(約3ヶ月)を推奨。既定 15000)")
    p.add_argument("--terminal-path", metavar="PATH",
                   help="live/export: 接続先MT5ターミナルのterminal64.exeパス。"
                        "省略時はOS既定の起動中ターミナルに接続 "
                        "(MetaTrader5パッケージの仕様: pathを明示しないと"
                        "どのターミナルに繋がるか不定。複数ターミナルを同時稼働"
                        "させる場合は手法ごとに別々のパスを明示すること)")
    p.add_argument("--server-utc-offset", type=int, metavar="HOURS",
                   help="live/export: ブローカーサーバー時刻 − UTC (時間)。省略時は "
                        "最新ティックから自動実測する(市場が開いていてティックが前進する"
                        "まで確定しない。週末の export では明示指定が必要)")
    p.add_argument("--journal", metavar="PATH",
                   help="live: 追記専用ジャーナル(イベントソーシング)JSONLの出力先 "
                        "(省略時 work/journal/<symbol>_<UTC日付>.jsonl)。"
                        "python -m infers.journal replay で点検・回帰検証できる")
    p.add_argument("--journal-eval", action="store_true",
                   help="live: 毎確定足の評価結果(どのゲートで見送りになったか)を "
                        "EVALイベントとしてジャーナルへ記録する。エントリーが長時間出ない "
                        "ときの『沈黙の中身』を tools/eval_funnel.py で可視化できる。"
                        "既定OFF(記録量が増えるため。取引ロジックには影響しない)")
    # HTMLレポート (backtest: 人間によるチェック用の可視化)
    p.add_argument("--report", metavar="DIR",
                   help="backtest: HTMLレポート出力ディレクトリ "
                        "(report.html + report_data.js を生成)")
    # 期間スライス (段階2.4: フルテスト/ライトテストを1データから切出し)
    p.add_argument("--from", dest="period_from", metavar="ISO8601",
                   help="backtest: 期間の開始(UTC、例 '2024-01-01')。"
                        "省略時はデータ先頭から")
    p.add_argument("--to", dest="period_to", metavar="ISO8601",
                   help="backtest: 期間の終了(UTC、例 '2025-01-01')。"
                        "省略時はデータ末尾まで")
    p.add_argument("--last", dest="period_last", metavar="PERIOD",
                   help="backtest: データ終端からの相対期間で切出し "
                        "(例 '1y'=ライトテスト、'5y'相当はフルテスト=既定の全期間)。"
                        "'--to' 併用時はそちらを終端とする")
    p.add_argument("--initial-capital", default="10000",
                   help="レポートの想定初期資金 USD (既定 10000)")
    p.add_argument("--contract-size", default="100",
                   help="1ロットの契約サイズ (XAUUSD=100oz 既定。BTCUSD は 1)")
    p.add_argument("--jpy-rate", default="150",
                   help="レポートの円換算レート USD/JPY (既定 150。UIでも変更可)")
    # スワップ (オーバーナイト金利)。値は USD / 1.0ロット / 夜 (負=コスト)
    p.add_argument("--swap-long-usd", default="-7.0",
                   help="買い保有スワップ USD/ロット/夜 (負=コスト。★ブローカー実値を設定)")
    p.add_argument("--swap-short-usd", default="-5.0",
                   help="売り保有スワップ USD/ロット/夜 (負=コスト)")
    p.add_argument("--rollover-hour", type=int, default=21,
                   help="ロールオーバー時刻 (UTC, 既定21)")
    p.add_argument("--swap-triple-weekday", type=int, default=2,
                   help="3倍スワップ曜日 (月=0..日=6, 既定=2=水)。-1で無効")
    p.add_argument("--no-swap", action="store_true",
                   help="スワップを計上しない (スプレッドのみ)")
    # マクロ方向フィルター (設計書 §1 フラクタル)
    p.add_argument("--macro-tf", choices=[t.value for t in Timeframe], default="D1",
                   help="方向を見定めるマクロ足 (既定 D1。手法ゲート1=D1/H1)。"
                        "エントリーはマクロ方向一致時のみ")
    p.add_argument("--wave2-tf", choices=[t.value for t in Timeframe], default="H4",
                   help="第2波(押し目)カウント用のマクロ足 (既定 H4)。方向TFと独立。"
                        "--macro-wave2 有効時に使用")
    p.add_argument("--rsi-mtf", nargs="*", choices=[t.value for t in Timeframe],
                   default=["H1", "D1"],
                   help="RSIマルチTFコンフルエンスに使う上位足 (既定 H1 D1。手法G2-⑤)")
    p.add_argument("--no-rsi-mtf", action="store_true",
                   help="RSIマルチTFを無効化 (M5単独RSIの従来挙動)")
    p.add_argument("--no-macro-filter", action="store_true",
                   help="マクロ方向フィルターを無効化 (ミクロ単独方向の従来挙動)")
    p.add_argument("--macro-k-atr-reversal", type=float, default=None,
                   help="マクロ方向TF(--macro-tf)専用のZigZag反転閾値 k (theta=k*ATR)。"
                        "省略時は既定値(1.5)を共有。"
                        "1.5はD1ダウのDOWN比率が最大化する較正点のため、"
                        "マクロのみ1.0前後への独立較正に使う")
    p.add_argument("--micro-k-atr-reversal", type=float, default=None,
                   help="ミクロ(M5)+押し目TF(--wave2-tf)+建値SL駆動用のZigZag反転閾値 k。"
                        "省略時は既定値(1.5)を共有。--macro-k-atr-reversal の較正が"
                        "ミクロのノイズ耐性・建値SL移動ロジックに干渉しないよう分離する")
    p.add_argument("--no-fib-score", action="store_true",
                   help="FIBをコンフルエンス・スコアから除外 (中核根拠の水増し防止)")
    p.add_argument("--depth-screen", action="store_true",
                   help="深さスクリーニング(下方40パーセントの深い押し目のみ)を有効化。本物の第2波が前提")
    p.add_argument("--depth-max", type=float, default=None,
                   help="深さスクリーニングの許容押し目位置 (第1波スパン下方割合)。"
                        "既定0.40=戻り60%%以上の深押しのみ。値を上げると浅い押し目を許容"
                        "(0.50=戻り50%%以上, 0.618=戻り38.2%%以上)。急騰相場の浅く速い"
                        "押し目買いの取りこぼし対策。40%%は原典に無いシステム独自値のため緩和可")
    p.add_argument("--depth-tier", action="store_true",
                   help="深さ階層化: 深い押し目(戻り≥60%%=--depth-max内)は2 familyで許可、"
                        "浅い押し目(戻り38.2〜60%%)は--shallow-min-families以上+上位足RSI極値の"
                        "壁を必須として例外許可。浅い押し目はSL距離が遠い分コンフルエンスで補う")
    p.add_argument("--depth-max-shallow", type=float, default=None,
                   help="深さ階層化/マクロ順応型の浅い押し目の外側境界 (既定0.618=戻り38.2%%)")
    p.add_argument("--macro-adaptive-depth", action="store_true",
                   help="マクロ順応型 深さ: D1 200SMAが順方向に傾く強トレンド時のみ浅い押し目"
                        "(depth_max_shallow まで)を通常2 familyで許可。弱/逆は深い押し目"
                        "(depth_max)のみ。depth_tierとは排他(逆発想: 強トレンドで緩める)")
    p.add_argument("--shallow-min-families", type=int, default=None,
                   help="深さ階層化の浅い押し目に要求するコンフルエンス数 (既定3)")
    p.add_argument("--expiry-recovery", action="store_true",
                   help="失効リカバリー: 打診指値が時間切れ(Expired)でキャンセルされた"
                        "瞬間にクールダウンを即時解除し、直後の確定足から未来裁量マップを"
                        "再計算・再提案する。価格×時間のピンポイント合流の取りこぼし対策。"
                        "無効化(シナリオ崩壊)では解除しない")
    p.add_argument("--macro-wave2", action="store_true",
                   help="上位足(--wave2-tf)のエリオットで第2波を判定 (M5ノイズでなく本物の波)")
    p.add_argument("--be-sl-macro-tf", action="store_true",
                   help="建値SL移動を上位足ダウ構造で行う (現結合では利確パイプラインが停止しやすい)")
    p.add_argument("--risk-pct", type=float, default=None,
                   help="1トレードの最大リスク比率 (例: 0.01=1%%)。"
                        "省略時は ProviderConfig.probe_volume_steps の固定ロット。"
                        "指定時は 残高 × risk_pct ÷ SL距離 で発注ステップ数を算出し、"
                        "残高が増えると自動的にロットが増える (資産比例型サイジング)")
    p.add_argument("--min-volume-steps", type=int, default=2,
                   help="--risk-pct 使用時の最低保証ロットステップ (既定2=Narrow Focus系の"
                        "半分利確が2step以上を要求するための下限)。"
                        "半分利確を持たない手法(例: atr_trend_scalp)+小口座では、"
                        "SL距離が広い足でリスク目標を大幅に超過する原因になるため 1 に"
                        "下げられる (VolumeSizerConfig.min_volume_steps を直接上書き)")
    p.add_argument("--min-be-distance", type=int, default=None,
                   help="建値SL移動を許可する最小スイング距離 (ticks)。"
                        "省略時は既定値(10 ticks=$0.10)。"
                        "大きくするとSL_AT_BE遷移が遅れPROBE状態が長くなり追撃が増える "
                        "(例: 200=2.00ドル, 500=5.00ドル)")
    p.add_argument("--be-offset", type=int, default=None,
                   help="建値SL = エントリー + この値(ticks)。"
                        "省略時は既定値(2 ticks=$0.02)。"
                        "大きくするとSL_AT_BE後のSL位置が遠くなりSL_AT_BE→ADD経路の余裕が増える")
    p.add_argument("--slippage-ticks", type=int, default=0,
                   help="backtest: 約定現実性ストレステスト用の逆方向スリッページ (ticks)。"
                        "成行参入・成行決済・保護SL/トレール決済の約定価格を不利方向へ"
                        "この本数ずらす (指値約定は指値契約のため対象外)。"
                        "既定0で従来挙動と1ティックも変わらない。トレール極小幅の手法"
                        "(atr_trend_scalp 等)の約定依存度を測る (改善プラン T7)")
    # 安全ガード (設計書 §11: デモ→最小ロットの段階を必ず踏む)
    p.add_argument("--demo", action="store_true", default=True,
                   help="デモ口座モード (既定)")
    p.add_argument("--allow-real-account", action="store_true",
                   help="実口座での稼働を明示的に許可 (デモ検証完了後のみ)")
    return p.parse_args(argv)


def resolve_ai_client(args: argparse.Namespace) -> str | None:
    """エントリーゲート方式の解決 (T15: ゲートの手法宣言化)。

    戻り値: "rule" | "none" | None (ゲートなし = TradingLoop はゲートを呼ばない)。

    優先順位:
      1. 明示された --ai-client (後方互換: 文書化済みコマンドの挙動を完全維持)
      2. --strategy 指定時: レジストリの StrategySpec.gate 宣言
         ("rule" → ルールゲート / None → ゲートなし)
      3. レガシー経路 (--strategy 無し / --provider): "rule" (従来の既定)
    """
    if args.ai_client is not None:
        return args.ai_client
    if args.strategy:
        from infers.strategies.registry import get_strategy
        return get_strategy(args.strategy).gate
    return "rule"


def _build_gateway(args: argparse.Namespace) -> "AiGateway | None":
    """既定はルールベースゲート (rule_judge.py: narrow_focus_v3.md §5 の
    決定論実装、$0・同期呼び出し可)。`--ai-client none` は手法非依存のパススルー
    (spec.md §5.7 案A。market_tpsl/smc_bos 等、Narrow Focus 固有特徴量を
    前提としない手法のバックテスト用)。どちらも純粋関数的でLLMを呼ばない
    ($0・例外なし。CLAUDE.md A-12: トレード判定にLLMは現在不使用)。

    解決結果 (resolve_ai_client) が None (手法がゲート不要を宣言) のときは
    ゲートウェイ自体を構築せず None を返す (T15)。"""
    resolved = resolve_ai_client(args)
    if resolved is None:
        return None
    if resolved == "none":
        from infers.ai.passthrough import PASSTHROUGH_POLICY, PassthroughLlmClient
        client = PassthroughLlmClient()
        policy = PASSTHROUGH_POLICY
    else:
        client = RuleBasedLlmClient()
        policy = RULE_POLICY
    return AiGateway(
        client=client,
        cache=VerdictCache(args.verdict_cache),
        policy=policy,
    )


def _print_gateway_stats(gateway: AiGateway) -> None:
    """AIゲートの判定内訳 (沈黙するNO_GOの可視化: CLAUDE.md 第11条)。"""
    if gateway.stats:
        print("ai_gateway: "
              + "  ".join(f"{k}={v}" for k, v in sorted(gateway.stats.items())))
    for reason, n in sorted(gateway.guardrail_reasons.items()):
        print(f"  guardrail[{reason}] = {n}")


def _with_progress(candles: list[Candle], *, every: int = 5000):
    """進捗をstderrへ周期的に出力しながらローソク足をyieldする (CLI層のみ)。"""
    total = len(candles)
    for i, candle in enumerate(candles, start=1):
        if i == 1 or i % every == 0 or i == total:
            pct = i / total * 100 if total else 100.0
            print(f"\rprogress: {i}/{total} ({pct:5.1f}%) "
                  f"{candle.open_time.isoformat()}", end="", file=sys.stderr)
        yield candle
    print(file=sys.stderr)


def _build_swap_config(args: argparse.Namespace):
    """ブローカー仕様 (USD/1.0ロット/夜) を tick*step 単位の SwapConfig へ換算。

    ticks_per_step = swap_usd_per_lot / (tick_size × contract_size)。
    """
    from infers.backtest.engine import SwapConfig
    if args.no_swap:
        return SwapConfig(enabled=False)
    spec = SYMBOLS[args.symbol]
    denom = spec.tick_size * Decimal(args.contract_size)
    long_tps = Decimal(args.swap_long_usd) / denom
    short_tps = Decimal(args.swap_short_usd) / denom
    return SwapConfig(enabled=True, long_ticks_per_step=long_tps,
                      short_ticks_per_step=short_tps,
                      rollover_hour_utc=args.rollover_hour,
                      triple_weekday=args.swap_triple_weekday)


def run_backtest(args: argparse.Namespace) -> int:
    from infers.backtest.engine import BacktestEngine, LedgerBroker
    from infers.data.exporter import load_history

    if not args.data:
        print("error: --mode backtest requires --data", file=sys.stderr)
        return 2

    candles = load_history(args.data, tf=Timeframe(args.tf))
    if args.period_from or args.period_to or args.period_last:
        from infers.backtest.slicing import slice_candles
        start = (datetime.fromisoformat(args.period_from).replace(tzinfo=timezone.utc)
                 if args.period_from else None)
        end = (datetime.fromisoformat(args.period_to).replace(tzinfo=timezone.utc)
               if args.period_to else None)
        before = len(candles)
        candles = slice_candles(candles, start=start, end=end, last=args.period_last)
        if not candles:
            print("error: --from/--to/--last でデータが0本になった", file=sys.stderr)
            return 2
        print(f"period: {before}本 → {len(candles)}本"
              f" ({candles[0].open_time.isoformat()} 〜 {candles[-1].open_time.isoformat()})")
    gateway = _build_gateway(args)

    recorder = None
    if args.report:
        from infers.backtest.report_html import BacktestRecorder, RecordingGateway
        from infers.core.report_spec import GENERIC_REPORT_SPEC
        # gateway=None (T15: 手法がゲート不要を宣言) のときは RecordingGateway で
        # 包まない — BacktestRecorder.gateway は None 許容 (_verdict_dict が None を返す)。
        if gateway is not None:
            gateway = RecordingGateway(gateway)
        report_spec = resolve_report_spec(args) or GENERIC_REPORT_SPEC
        recorder = BacktestRecorder(gateway=gateway, report_spec=report_spec)

    swap = _build_swap_config(args)
    if swap.enabled:
        print(f"swap: 買い {args.swap_long_usd} / 売り {args.swap_short_usd} "
              f"USD/ロット/夜, ロールオーバー {args.rollover_hour}:00 UTC, "
              f"水3倍={'有' if args.swap_triple_weekday == 2 else '無'} "
              f"(★ブローカー実値を --swap-long-usd/--swap-short-usd で設定のこと)")
    fsm_config = FsmConfig(
        min_be_distance_ticks=(args.min_be_distance
                               if args.min_be_distance is not None
                               else DEFAULT_FSM.min_be_distance_ticks),
        be_offset_ticks=(args.be_offset
                         if args.be_offset is not None
                         else DEFAULT_FSM.be_offset_ticks),
        breakout_buffer_ticks=DEFAULT_FSM.breakout_buffer_ticks,
    )
    # 可変ロットサイジング (--risk-pct 指定時のみ有効)
    volume_sizer = None
    equity_provider = None
    if args.risk_pct is not None:
        from infers.backtest.engine import BacktestEquityProvider
        from infers.execution.risk import VolumeSizer, VolumeSizerConfig
        spec = SYMBOLS[args.symbol]
        tick_value = spec.tick_size * Decimal(args.contract_size) * spec.lot_step
        volume_sizer = VolumeSizer(VolumeSizerConfig(
            risk_pct=Decimal(str(args.risk_pct)),
            tick_value_per_step=tick_value,
            min_volume_steps=args.min_volume_steps,
            max_volume_steps=DEFAULT_RISK.max_position_volume_steps,
        ))
        equity_provider = BacktestEquityProvider(
            initial_capital_usd=Decimal(args.initial_capital),
            tick_value_usd_per_step=tick_value,
        )
        print(f"sizing: risk={args.risk_pct*100:.1f}%  "
              f"tick_value={tick_value} USD/tick/step  "
              f"initial_equity={args.initial_capital} USD")
    engine = BacktestEngine(
        broker=LedgerBroker(spread_ticks=2, min_stop_distance_ticks=5,
                            slippage_ticks=args.slippage_ticks),
        gateway=gateway,
        risk=RiskManager(DEFAULT_RISK),
        fsm_config=fsm_config,
        swap=swap,
        volume_sizer=volume_sizer,
        equity_provider=equity_provider,
        execution_factory=build_execution_factory(args),
    )
    report = engine.run(_with_progress(candles), build_provider(args),
                        recorder=recorder)

    swap_ts = sum(t.swap_tick_steps for t in report.trades)
    print(f"bars={len(candles)} trades={len(report.trades)}")
    print(f"total_pnl={report.total_pnl_tick_steps} tick*steps "
          f"(うちスワップ {swap_ts})")
    print(f"profit_factor={report.profit_factor}  win_rate={report.win_rate}")
    print(f"be_sl_exit_rate={report.be_sl_exit_rate}  "
          f"max_dd={report.max_drawdown_tick_steps}")
    if gateway is not None:
        _print_gateway_stats(gateway)

    if args.report and recorder is not None:
        from infers.backtest.report_html import write_html_report
        html = write_html_report(
            args.report, candles=candles, report=report, recorder=recorder,
            spec=SYMBOLS[args.symbol], tf=Timeframe(args.tf),
            initial_capital=Decimal(args.initial_capital),
            contract_size=Decimal(args.contract_size),
            jpy_rate=Decimal(args.jpy_rate))
        print(f"report: {html} (ブラウザで開いてください)")
    return 0


def run_replay(args: argparse.Namespace) -> int:
    """Pass2: verdict_cache 参照のみの最終リプレイ (run_backtest と同一実装)。"""
    return run_backtest(args)


def _open_live_journal(args: argparse.Namespace):
    """ライブ用の追記専用ジャーナルを開き、SESSIONイベントを記録して返す。"""
    from infers.journal import JournalWriter

    if args.journal:
        path = Path(args.journal)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        path = Path("work/journal") / f"{args.symbol}_{stamp}.jsonl"
    journal = JournalWriter(path)
    journal.record("SESSION", {
        "mode": "live",
        "symbol": args.symbol,
        "tf": args.tf,
        "ai_client": resolve_ai_client(args),
        "demo": bool(args.demo and not args.allow_real_account),
        "prompt_version": PROMPT_VERSION,
        "strategy": getattr(args, "strategy", None),
        **_git_state(),
    })
    return journal


def _git_state() -> dict:
    """稼働中コードの特定用 (起動元のコミットと未コミット変更の有無)。取得失敗は None。"""
    import subprocess

    repo = Path(__file__).resolve().parents[2]
    def _git(*a: str) -> str | None:
        try:
            out = subprocess.run(["git", "-C", str(repo), *a], capture_output=True,
                                 text=True, timeout=10, check=True)
            return out.stdout.strip()
        except Exception:                       # noqa: BLE001 — git が無い環境では記録しない
            return None
    status = _git("status", "--porcelain", "--untracked-files=no")
    return {"git_commit": _git("rev-parse", "HEAD"),
            "git_branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "git_dirty": None if status is None else bool(status)}


def _server_offset(args: argparse.Namespace) -> "timedelta | None":
    hours = getattr(args, "server_utc_offset", None)
    return None if hours is None else timedelta(hours=hours)


def run_live(args: argparse.Namespace) -> int:
    from infers.data.mt5_feed import MT5Feed
    from infers.execution.mt5_adapter import MT5LiveBroker

    if not args.demo and not args.allow_real_account:
        print("error: real account requires --allow-real-account "
              "(デモ口座での検証を先に完了してください)", file=sys.stderr)
        return 2

    import faulthandler
    import traceback

    # 異常終了の痕跡を残す: stdout は行バッファ (リダイレクト先でも即書き出し)、
    # ネイティブクラッシュは faulthandler が stderr へダンプする。
    sys.stdout.reconfigure(line_buffering=True)
    faulthandler.enable()

    spec = SYMBOLS[args.symbol]
    feed = MT5Feed(terminal_path=args.terminal_path,
                   server_utc_offset=_server_offset(args))
    broker = MT5LiveBroker(spec)
    journal = _open_live_journal(args)
    print(f"journal: {journal.path}")
    try:
        return _run_live_session(args, spec, feed, broker, journal)
    except BaseException as e:
        if not isinstance(e, KeyboardInterrupt):
            # 未捕捉例外で終了する前に、原因をジャーナルへ残す (ログは上書きされうる)
            journal.record("CRASH", {
                "type": type(e).__name__, "message": str(e),
                "traceback": traceback.format_exc(limit=30)})
        raise
    finally:
        feed.close()
        journal.close()


def _run_live_session(args, spec, feed, broker, journal) -> int:
    from infers.execution.mt5_adapter import LiveRunner

    feed.connect()
    broker.connect(terminal_path=args.terminal_path)
    # サーバー時刻オフセットの確定を待つ (休場中はティックが進まないので開場まで待機)。
    # 確定前にウォームアップすると全履歴の時刻ラベルが壊れる (週末起動バグ)。
    if feed.offset_pending:
        print("waiting for market open to determine server UTC offset...",
              file=sys.stderr)
        journal.record("OFFSET_WAIT", {})
        feed.wait_for_offset(spec)
    journal.record("FEED_OFFSET", {"server_utc_offset_hours":
                                   feed.server_utc_offset.total_seconds() / 3600})
    # 可変ロットサイジング (--risk-pct 指定時のみ有効)
    live_sizer = None
    if args.risk_pct is not None:
        from infers.execution.risk import VolumeSizer, VolumeSizerConfig
        tick_val = broker.tick_value_per_step(args.symbol)
        live_sizer = VolumeSizer(VolumeSizerConfig(
            risk_pct=Decimal(str(args.risk_pct)),
            tick_value_per_step=tick_val,
            min_volume_steps=args.min_volume_steps,
            max_volume_steps=DEFAULT_RISK.max_position_volume_steps,
        ))
        print(f"sizing: risk={args.risk_pct*100:.1f}%  tick_value={tick_val}/tick/step")
    runner = LiveRunner(
        feed=feed, spec=spec, tf=Timeframe(args.tf), broker=broker,
        provider=build_provider(args),
        gateway=_build_gateway(args),
        risk=RiskManager(DEFAULT_RISK), fsm_config=DEFAULT_FSM,
        journal=journal,
        warm_bars=args.warm_bars,
        volume_sizer=live_sizer,
        execution_factory=build_execution_factory(args),
        journal_eval=args.journal_eval,
    )
    try:
        bars = runner.run(max_bars=args.max_bars)
        print(f"processed {bars} closed bars")
    except KeyboardInterrupt:
        # Ctrl+C による手動停止 (デモ運用のニュース回避手順)。安全停止へ回す。
        print("interrupted — graceful shutdown", file=sys.stderr)
    # 正常終了・中断のいずれでも安全停止: 未約定の打診指値を取消し残玉を手仕舞う
    # (停止後にブローカーへ孤児注文を残さない。デモのニュース前停止が安全になる)。
    closed = runner.shutdown()
    if closed:
        print(f"shutdown: cancelled/closed {len(closed)} position(s): {closed}")
    return 0


def run_export(args: argparse.Namespace) -> int:
    from infers.data.exporter import export_history
    from infers.data.mt5_feed import MT5Feed

    if not args.data:
        print("error: --mode export requires --data (出力パス)", file=sys.stderr)
        return 2

    spec = SYMBOLS[args.symbol]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=365 * args.years)
    with MT5Feed(terminal_path=args.terminal_path,
                 server_utc_offset=_server_offset(args)) as feed:
        count = export_history(feed, spec, Timeframe(args.tf), start, end, args.data)
    print(f"exported {count} candles -> {args.data}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.mode == "backtest":
        return run_backtest(args)
    if args.mode == "replay":
        return run_replay(args)
    if args.mode == "export":
        return run_export(args)
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())

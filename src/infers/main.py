"""INFERS 統合エントリーポイント (フェーズ8〜10)。

  python -m infers.main --mode judge    --data data/xauusd_m5.parquet   # Pass1
  python -m infers.main --mode judge    --ingest batch_results.jsonl    # 結果取込
  python -m infers.main --mode replay   --data data/xauusd_m5.parquet   # Pass2
  python -m infers.main --mode backtest --data data/xauusd_m5.parquet   # replay同義
  python -m infers.main --mode live --demo
  python -m infers.main --mode export --data out.parquet --years 5

2パスバックテスト (CLAUDE.md 第16条):
  - backtest/replay はキャッシュ専用 — 同期ループ内で messages.create を呼ばない。
    キャッシュ未解決の判断はガードレール NO_GO になり、件数がレポートに出る
  - judge: L0決定論スイープで未解決の裁定イベントを Batch API リクエスト
    (JSONL) に書き出す。--submit で投入、--fetch <batch_id> で結果を
    verdict_cache へ永続化 (--ingest はローカル結果ファイルからの取込)

安全ガード:
  - live モードは --demo が既定。実口座は --allow-real-account の明示が必須
  - SignalProvider は "module:factory" 形式で注入する。既定は
    InfersSignalProvider。NullProvider (監視のみ・発注ゼロ) へ差し替え可能
  - APIキー・口座資格情報は環境変数のみ (ANTHROPIC_API_KEY 等)
"""

from __future__ import annotations

import argparse
import importlib
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from infers.ai.gateway import (
    PROMPT_VERSION, AiGateway, AnthropicLlmClient, EscalationPolicy, VerdictCache,
)
from infers.ai.rule_judge import RuleBasedLlmClient
from infers.core.loop import ProviderOutput, SignalProvider
from infers.data.models import Candle, SymbolSpec, Timeframe
from infers.execution.risk import RiskConfig, RiskManager
from infers.execution.sm import FsmConfig

SYMBOLS: dict[str, SymbolSpec] = {
    "XAUUSD": SymbolSpec(name="XAUUSD", tick_size=Decimal("0.01"),
                         lot_step=Decimal("0.01"), digits=2),
    "BTCUSD": SymbolSpec(name="BTCUSD", tick_size=Decimal("0.01"),
                         lot_step=Decimal("0.01"), digits=2),
}

DEFAULT_POLICY = EscalationPolicy(score_l1=Decimal(2), score_l2=Decimal(4),
                                  ambiguity_gray=Decimal("0.1"), l2_daily_call_cap=3)
# ルールベースゲートは $0 のため L2 予算キャップが不要 (キャップは Claude 課金対策)。
# キャップを残すと L2_AFTER_L1 案件が日次4件目以降 GUARDRAIL NO_GO に化ける。
RULE_POLICY = EscalationPolicy(score_l1=Decimal(2), score_l2=Decimal(4),
                               ambiguity_gray=Decimal("0.1"),
                               l2_daily_call_cap=1_000_000)
DEFAULT_RISK = RiskConfig(max_position_volume_steps=4, max_total_volume_steps=8,
                          max_spread_ticks=50, daily_loss_limit_tick_steps=50_000)
DEFAULT_FSM = FsmConfig(min_be_distance_ticks=10, be_offset_ticks=2,
                        breakout_buffer_ticks=10)


class CacheMissError(LookupError):
    """replay/backtest 中に verdict_cache 未解決の判断へ到達した。"""


class CacheOnlyClient:
    """バックテスト用クライアント: LLMを呼ばず常にキャッシュミスを通知する。

    AiGateway のガードレールが NO_GO (DEFAULT NO-TRADE) に変換するため、
    キャッシュ未解決の判断は安全側へ倒れる (CLAUDE.md 第1・16条)。
    未解決件数はゲートウェイの guardrail_reasons に集計される。
    """

    def judge(self, request, tier: str):  # noqa: ARG002 — 契約上のシグネチャ
        raise CacheMissError("verdict cache miss (run --mode judge first)")


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
    from infers.strategy.provider import InfersSignalProvider, ProviderConfig
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="infers", description="INFERS trading system")
    p.add_argument("--mode", choices=["backtest", "judge", "replay", "live", "export"],
                   required=True)
    p.add_argument("--symbol", choices=sorted(SYMBOLS), default="XAUUSD")
    p.add_argument("--tf", choices=[t.value for t in Timeframe], default="M5")
    p.add_argument("--data", help="Parquetパス (backtest: 入力 / export: 出力)")
    p.add_argument("--provider",
                   help="SignalProvider factory ('module:attr')。"
                        "省略時は InfersSignalProvider (フルパイプライン)")
    p.add_argument("--verdict-cache", default="work/cache/verdicts.sqlite3")
    p.add_argument("--ai-client", choices=["rule", "claude"], default="rule",
                   help="エントリーゲートの判定方式。既定は rule_judge.py の"
                        "決定論ルール ($0, narrow_focus_v3.md §5 実装)。"
                        "claude は旧Claude L1/L2ゲート (Batches API 経由)")
    p.add_argument("--system-prompt-file", help="LLM用システムプロンプト (凍結ファイル)")
    p.add_argument("--years", type=int, default=5, help="export: 取得年数")
    # 2パスバックテスト (judge = Pass1 / replay = Pass2。CLAUDE.md 第16条)
    p.add_argument("--batch", action="store_true",
                   help="judge: 未解決の裁定イベントをJSONLへ書き出す (既定動作の明示)")
    p.add_argument("--tier", choices=["L1", "L2"],
                   help="judge: 2段階収集。L1 (Haiku) を先に投入・取込し、"
                        "L1=GO のプランのみ --tier L2 (Fable 5) を投入する"
                        "(省略時は一括収集 = L2課金が最大)")
    p.add_argument("--batch-file", default="batch_requests.jsonl",
                   help="judge: Batch APIリクエストJSONLの出力パス")
    p.add_argument("--submit", action="store_true",
                   help="judge: 書き出したリクエストを Batches API へ投入する")
    p.add_argument("--fetch", metavar="BATCH_ID",
                   help="judge: Batches API から結果を取得し verdict_cache へ永続化")
    p.add_argument("--save-raw", metavar="PATH",
                   help="judge --fetch: 取得した生結果JSONLの保存先 "
                        "(省略時は <batch_id>.results.jsonl。常に保存される)")
    p.add_argument("--ingest", metavar="RESULTS_JSONL",
                   help="judge: ローカルの結果JSONLを verdict_cache へ取込")
    p.add_argument("--limit", type=int, metavar="N",
                   help="judge: 投入リクエストを先頭N件に制限する "
                        "(本番投入前の少額パイロット検証用)")
    p.add_argument("--max-bars", type=int, help="live: 処理バー数上限 (検証用)")
    p.add_argument("--journal", metavar="PATH",
                   help="live: 追記専用ジャーナル(イベントソーシング)JSONLの出力先 "
                        "(省略時 work/journal/<symbol>_<UTC日付>.jsonl)。"
                        "python -m infers.journal replay で点検・回帰検証できる")
    # HTMLレポート (backtest: 人間によるチェック用の可視化)
    p.add_argument("--report", metavar="DIR",
                   help="backtest: HTMLレポート出力ディレクトリ "
                        "(report.html + report_data.js を生成)")
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
    # 安全ガード (設計書 §11: デモ→最小ロットの段階を必ず踏む)
    p.add_argument("--demo", action="store_true", default=True,
                   help="デモ口座モード (既定)")
    p.add_argument("--allow-real-account", action="store_true",
                   help="実口座での稼働を明示的に許可 (デモ検証完了後のみ)")
    return p.parse_args(argv)


_DEFAULT_SYSTEM_PROMPT_PATH = (
    Path(__file__).resolve().parent / "ai" / "prompts" / "narrow_focus_v3.md")


def _load_system_prompt(args: argparse.Namespace) -> str:
    path = (Path(args.system_prompt_file) if args.system_prompt_file
            else _DEFAULT_SYSTEM_PROMPT_PATH)
    return path.read_text(encoding="utf-8")


def _build_gateway(args: argparse.Namespace, *, cache_only: bool) -> AiGateway:
    """既定はルールベースゲート (rule_judge.py: narrow_focus_v3.md §5 の
    決定論実装、$0・同期呼び出し可)。`--ai-client claude` で旧LLMゲートに
    切替可能 — その場合 cache_only=True (backtest/replay) では LLM
    クライアントを配線しない (CLAUDE.md 第16条: 同期ループ内で
    messages.create を呼ばない)。"""
    if args.ai_client == "claude":
        client = CacheOnlyClient() if cache_only else AnthropicLlmClient(
            _load_system_prompt(args))
        policy = DEFAULT_POLICY
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
    misses = sum(n for r, n in gateway.guardrail_reasons.items()
                 if "CacheMissError" in r)
    if misses:
        print(f"warning: {misses} 件の判断が verdict_cache 未解決 (NO_GO扱い)。"
              f" --mode judge → Batch実行 → --mode judge --fetch/--ingest"
              f" で解決してから replay してください", file=sys.stderr)


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
    gateway = _build_gateway(args, cache_only=True)

    recorder = None
    if args.report:
        from infers.backtest.report_html import BacktestRecorder, RecordingGateway
        gateway = RecordingGateway(gateway)
        recorder = BacktestRecorder(gateway=gateway)

    swap = _build_swap_config(args)
    if swap.enabled:
        print(f"swap: 買い {args.swap_long_usd} / 売り {args.swap_short_usd} "
              f"USD/ロット/夜, ロールオーバー {args.rollover_hour}:00 UTC, "
              f"水3倍={'有' if args.swap_triple_weekday == 2 else '無'} "
              f"(★ブローカー実値を --swap-long-usd/--swap-short-usd で設定のこと)")
    engine = BacktestEngine(
        broker=LedgerBroker(spread_ticks=2, min_stop_distance_ticks=5),
        gateway=gateway,
        risk=RiskManager(DEFAULT_RISK),
        fsm_config=DEFAULT_FSM,
        swap=swap,
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


def run_judge(args: argparse.Namespace) -> int:
    """Pass1: 裁定イベント収集→JSONL書き出し (+投入/取込) (設計書 §7.4)。"""
    from infers.ai.batch import (
        ingest_batch_results, ingest_batch_results_file, write_batch_file,
    )
    from infers.backtest.engine import BacktestEngine
    from infers.data.exporter import load_history

    from collections import Counter

    cache = VerdictCache(args.verdict_cache)

    def _report_ingest(count: int, stats: Counter, source: str) -> None:
        print(f"ingested {count} verdicts from {source} -> {args.verdict_cache}")
        breakdown = "  ".join(f"{k}={v}" for k, v in sorted(stats.items()))
        print(f"batch results: {breakdown}")
        if stats.get("parse_failed"):
            print(f"warning: {stats['parse_failed']} 件が Verdict スキーマとして"
                  f"解釈できず破棄されました。structured outputs なしで投入された"
                  f"旧バッチの可能性があります。--mode judge で再収集→再投入して"
                  f"ください", file=sys.stderr)
        if stats.get("errored"):
            print(f"warning: {stats['errored']} 件がAPI側でエラー終了しています",
                  file=sys.stderr)

    if args.fetch:
        from pathlib import Path

        import anthropic
        client = anthropic.Anthropic()
        results = client.messages.batches.results(args.fetch)
        # 生結果を必ず先にディスクへ退避する (取込で破棄されても再解析できる
        # ように。Batch結果はサーバ側で約29日しか保持されないため、課金済み
        # データの回収可能性を手元に確保しておく)。
        raw_lines = [item.model_dump_json() for item in results]
        raw_path = args.save_raw or f"{args.fetch}.results.jsonl"
        Path(raw_path).write_text(
            "\n".join(raw_lines) + ("\n" if raw_lines else ""), encoding="utf-8")
        print(f"saved {len(raw_lines)} raw results -> {raw_path}")
        stats: Counter = Counter()
        count = ingest_batch_results(raw_lines, cache, stats=stats)
        _report_ingest(count, stats, f"batch {args.fetch}")
        return 0

    if args.ingest:
        stats = Counter()
        count = ingest_batch_results_file(args.ingest, cache, stats=stats)
        _report_ingest(count, stats, args.ingest)
        return 0

    if not args.data:
        print("error: --mode judge requires --data", file=sys.stderr)
        return 2

    candles = load_history(args.data, tf=Timeframe(args.tf))
    stage = args.tier or "ALL"
    pending = BacktestEngine.collect_judgements(
        _with_progress(candles), build_provider(args),
        policy=DEFAULT_POLICY, cache=cache, tier=stage)
    total_pending = len(pending)
    if args.limit is not None and args.limit < total_pending:
        pending = pending[:args.limit]
        print(f"[pilot] {total_pending} 件中 先頭 {len(pending)} 件のみ投入します "
              f"(少額検証)")
    count = write_batch_file(pending, args.batch_file,
                             system_prompt=_load_system_prompt(args))
    print(f"collected {count} unresolved judgements (tier={stage}) "
          f"-> {args.batch_file}")

    if not count:
        if stage == "L1":
            print("L1 は全件解決済み。次: --tier L2 で第2弾を収集してください")
        else:
            print("verdict_cache は全件解決済み。--mode replay へ進めます")
        return 0

    if args.submit:
        import json as _json

        import anthropic
        from pathlib import Path
        requests = [_json.loads(line) for line
                    in Path(args.batch_file).read_text(encoding="utf-8").splitlines()
                    if line.strip()]
        client = anthropic.Anthropic()
        batch = client.messages.batches.create(requests=requests)
        print(f"submitted batch: {batch.id} (status={batch.processing_status})")
        print(f"完了後: python -m infers.main --mode judge --fetch {batch.id} "
              f"--verdict-cache {args.verdict_cache}")
        if stage == "L1":
            print("取込後、第2弾: --mode judge --tier L2 --submit で "
                  "L1=GO のプランのみ L2 (Fable 5) を投入してください")
    else:
        print("次: --submit で Batches API へ投入するか、結果JSONLを "
              "--ingest で取込んでください")
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
        "ai_client": args.ai_client,
        "demo": bool(args.demo and not args.allow_real_account),
        "prompt_version": PROMPT_VERSION,
    })
    return journal


def run_live(args: argparse.Namespace) -> int:
    from infers.data.mt5_feed import MT5Feed
    from infers.execution.mt5_adapter import LiveRunner, MT5LiveBroker

    if not args.demo and not args.allow_real_account:
        print("error: real account requires --allow-real-account "
              "(デモ口座での検証を先に完了してください)", file=sys.stderr)
        return 2

    spec = SYMBOLS[args.symbol]
    feed = MT5Feed()
    broker = MT5LiveBroker(spec)
    journal = _open_live_journal(args)
    print(f"journal: {journal.path}")
    feed.connect()
    broker.connect()
    try:
        runner = LiveRunner(
            feed=feed, spec=spec, tf=Timeframe(args.tf), broker=broker,
            provider=build_provider(args),
            gateway=_build_gateway(args, cache_only=False),
            risk=RiskManager(DEFAULT_RISK), fsm_config=DEFAULT_FSM,
            journal=journal,
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
    finally:
        feed.close()
        journal.close()


def run_export(args: argparse.Namespace) -> int:
    from infers.data.exporter import export_history
    from infers.data.mt5_feed import MT5Feed

    if not args.data:
        print("error: --mode export requires --data (出力パス)", file=sys.stderr)
        return 2

    spec = SYMBOLS[args.symbol]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=365 * args.years)
    with MT5Feed() as feed:
        count = export_history(feed, spec, Timeframe(args.tf), start, end, args.data)
    print(f"exported {count} candles -> {args.data}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.mode == "backtest":
        return run_backtest(args)
    if args.mode == "judge":
        return run_judge(args)
    if args.mode == "replay":
        return run_replay(args)
    if args.mode == "export":
        return run_export(args)
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())

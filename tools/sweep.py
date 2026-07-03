"""汎用パラメータ・スイープ・ハーネス(手法非依存、改善プランT9)。

atr_trend_scalp v1.1 チューニングで使った「`run_backtest` と同一条件をビット一致で
再現するインプロセス・スイープ(1構成≈数秒)」を、任意の登録手法へ使える汎用ツールへ
昇格したもの。

設計上の不変条件:
  - **一方向依存**: 本ツールは `infers.main`/`infers.backtest.engine` 等を import するが、
    その逆(バックテスト本体からの本ツールへの import)は無い。`infers.main._build_gateway`/
    `_build_swap_config` を直接呼ぶことで、CLI(`python -m infers.main --mode backtest`)と
    ゲート・スワップの構築ロジックを完全に共有し、二重実装によるビット一致崩れを避ける。
  - **自己検証**: スイープ本体を回す前に、既定パラメータ(overrides無し)でベースラインを
    1回実行し、呼び出し側が指定した黄金値(トレード数/PF/最大DD)と完全一致するか検証する。
    不一致ならスイープ全体を中止する(誤った土台の上に実験を積む事故を防ぐ)。
  - **年次分解の強制**: 各候補構成の結果に `annual_breakdown.py` を必ず適用し、
    単年純益集中・年次フロア割れを機械警告する(CLAUDE.md 定石6)。
  - **非単調振動の検出**: 同一パラメータ軸(OFAT)上で隣接値間のPFが2回以上向きを変えたら
    「過剰適合疑い」を明記し、単発の最良値を採用推奨として出さない
    (smc_bos の rr_target 論点Bで確認された過剰適合パターンの型)。

使い方(atr_trend_scalp v1.1 チューニングの再現例):

    python tools/sweep.py --data data/xauusd_m5.parquet \\
      --provider-class infers.strategies.atr_trend_scalp.provider:AtrTrendScalpProvider \\
      --strategy atr_trend_scalp --ai-client none \\
      --golden-trades 6656 --golden-pf 1.173164608 --golden-max-dd 25783 \\
      --base tp1_atr_mult=1.0 --base tp2_atr_mult=2.0 --base trail_atr_mult=0.5 \\
      --param vol_mult=1.2,1.45,1.6

トレード判定コードには一切触れない(既存 provider/execution/engine を差し替えなしで
呼ぶだけ)。
"""

from __future__ import annotations

import argparse
import importlib
import itertools
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from annual_breakdown import annual_breakdown, evaluate_warnings  # noqa: E402

from infers.backtest.engine import BacktestEngine, BacktestReport, LedgerBroker
from infers.core.models import Candle, SymbolSpec, Timeframe
from infers.data.exporter import load_history
from infers.execution.risk import RiskManager
from infers.main import (  # noqa: E402 — 本体の構築ロジックをそのまま再利用(二重実装回避)
    DEFAULT_FSM, DEFAULT_RISK, SYMBOLS, _build_gateway, _build_swap_config,
)


class SweepBaselineMismatch(RuntimeError):
    """ベースライン自己検証が黄金値と不一致(スイープを中止)。"""


@dataclass(frozen=True)
class SweepConfig:
    label: str
    overrides: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SweepResult:
    label: str
    trades: int
    profit_factor: Decimal | None
    win_rate: Decimal
    max_dd_tick_steps: int
    net_profit_ts: int
    annual: dict[int, dict]
    warnings: list[str]


class _LightRecorder:
    """年次分解に必要な最小限(損益・退出時刻)だけを集める軽量レコーダー。

    `backtest/report_html.py` の `BacktestRecorder` と違い、HTML描画用の
    プラン詳細・ジャーナル・verdict は集めない(スイープの反復速度を優先)。
    """

    def __init__(self) -> None:
        self.trades: list[dict] = []

    def on_trade_closed(self, record, ledger, fsm, plan) -> None:  # noqa: ARG002
        exit_t = ledger.exit_times[-1] if ledger.exit_times else None
        self.trades.append({
            "pnl_ts": record.pnl_tick_steps,
            "exit_time": int(exit_t.timestamp()) if exit_t is not None else None,
        })

    def on_unfilled(self, position_id, fsm, plan) -> None:  # noqa: ARG002
        pass


def _cli_ns(*, symbol: str, ai_client: str, verdict_cache: str, contract_size: str,
           swap_long_usd: str, swap_short_usd: str, rollover_hour: int,
           swap_triple_weekday: int, no_swap: bool) -> SimpleNamespace:
    """`infers.main._build_gateway`/`_build_swap_config` が要求する属性だけを持つ
    最小 Namespace。本体の関数をそのまま呼ぶための橋渡し(ロジックの二重実装を避ける)。"""
    return SimpleNamespace(
        ai_client=ai_client, verdict_cache=verdict_cache, symbol=symbol,
        contract_size=contract_size, swap_long_usd=swap_long_usd,
        swap_short_usd=swap_short_usd, rollover_hour=rollover_hour,
        swap_triple_weekday=swap_triple_weekday, no_swap=no_swap,
    )


def run_one(candles: list[Candle], *, provider: Any, execution_factory: Any,
           spec: SymbolSpec, ai_client: str = "none", verdict_cache: str = ":memory:",
           spread_ticks: int = 2, min_stop_distance_ticks: int = 5,
           slippage_ticks: int = 0, contract_size: str = "100",
           swap_long_usd: str = "-7.0", swap_short_usd: str = "-5.0",
           rollover_hour: int = 21, swap_triple_weekday: int = 2,
           no_swap: bool = False) -> tuple[BacktestReport, list[dict]]:
    """`main.run_backtest` と同一構成(gateway/risk/fsm/swap/broker)をインプロセスで
    1回実行する。返り値は (BacktestReport, 年次分解用トレード列)。"""
    ns = _cli_ns(symbol=spec.name, ai_client=ai_client, verdict_cache=verdict_cache,
                contract_size=contract_size, swap_long_usd=swap_long_usd,
                swap_short_usd=swap_short_usd, rollover_hour=rollover_hour,
                swap_triple_weekday=swap_triple_weekday, no_swap=no_swap)
    gateway = _build_gateway(ns)
    swap = _build_swap_config(ns)
    engine = BacktestEngine(
        broker=LedgerBroker(spread_ticks=spread_ticks,
                            min_stop_distance_ticks=min_stop_distance_ticks,
                            slippage_ticks=slippage_ticks),
        gateway=gateway, risk=RiskManager(DEFAULT_RISK), fsm_config=DEFAULT_FSM,
        swap=swap, execution_factory=execution_factory,
    )
    recorder = _LightRecorder()
    report = engine.run(candles, provider, recorder=recorder)
    return report, recorder.trades


def _coerce_value(default: Any, raw: str) -> Any:
    """provider コンストラクタの既定値の型に合わせて文字列を変換する。"""
    if isinstance(default, Decimal):
        return Decimal(raw)
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, Timeframe):
        return Timeframe(raw)
    return raw


def build_provider_instance(provider_cls: type, *, symbol: str, tf: Timeframe,
                            overrides: dict[str, str]) -> Any:
    """provider クラスを、既定値の型へ合わせて上書きしたパラメータで構築する。

    パラメータ名は provider の `__init__` シグネチャから解決するため、
    存在しないパラメータ名を指定すると即座にエラーになる(タイプミスの早期検出)。
    """
    import inspect
    sig = inspect.signature(provider_cls.__init__)
    kwargs: dict[str, Any] = {}
    for name, raw in overrides.items():
        if name not in sig.parameters:
            raise ValueError(
                f"{provider_cls.__name__} has no parameter {name!r} "
                f"(利用可能: {sorted(p for p in sig.parameters if p not in ('self',))})")
        kwargs[name] = _coerce_value(sig.parameters[name].default, raw)
    return provider_cls(symbol=symbol, tf=tf, **kwargs)


def self_verify_baseline(candles: list[Candle], *, provider_cls: type, execution_factory: Any,
                         spec: SymbolSpec, tf: Timeframe, ai_client: str = "none",
                         golden_trades: int | None = None, golden_pf: str | None = None,
                         golden_max_dd: int | None = None,
                         **run_kwargs: Any) -> tuple[BacktestReport, list[dict]]:
    """既定パラメータ(overrides無し)でベースラインを実行し、黄金値と突合する。

    黄金値が1つも指定されなければ検証はスキップされ、結果を返すのみ(参考実行)。
    どれか1つでも不一致なら `SweepBaselineMismatch` を送出しスイープ全体を止める。
    """
    provider = provider_cls(symbol=spec.name, tf=tf)
    report, trades = run_one(candles, provider=provider, execution_factory=execution_factory,
                             spec=spec, ai_client=ai_client, **run_kwargs)
    mismatches: list[str] = []
    if golden_trades is not None and len(report.trades) != golden_trades:
        mismatches.append(f"trades: got {len(report.trades)}, expected {golden_trades}")
    if golden_pf is not None:
        pf_str = str(report.profit_factor) if report.profit_factor is not None else None
        if pf_str != golden_pf:
            mismatches.append(f"profit_factor: got {pf_str}, expected {golden_pf}")
    if golden_max_dd is not None and report.max_drawdown_tick_steps != golden_max_dd:
        mismatches.append(
            f"max_dd: got {report.max_drawdown_tick_steps}, expected {golden_max_dd}")
    if mismatches:
        raise SweepBaselineMismatch(
            "ベースライン自己検証に失敗(黄金値と不一致。スイープを中止): "
            + "; ".join(mismatches))
    return report, trades


def sweep(candles: list[Candle], *, provider_cls: type, execution_factory: Any,
         spec: SymbolSpec, tf: Timeframe, configs: list[SweepConfig],
         ai_client: str = "none", **run_kwargs: Any) -> list[SweepResult]:
    """候補構成を順に実行し、年次分解つきの結果一覧を返す。"""
    results: list[SweepResult] = []
    for cfg in configs:
        provider = build_provider_instance(provider_cls, symbol=spec.name, tf=tf,
                                           overrides=cfg.overrides)
        report, trades = run_one(candles, provider=provider,
                                 execution_factory=execution_factory, spec=spec,
                                 ai_client=ai_client, **run_kwargs)
        annual = annual_breakdown({"trades": trades})
        warnings = evaluate_warnings(annual)
        results.append(SweepResult(
            label=cfg.label, trades=len(report.trades),
            profit_factor=report.profit_factor, win_rate=report.win_rate,
            max_dd_tick_steps=report.max_drawdown_tick_steps,
            net_profit_ts=report.total_pnl_tick_steps, annual=annual, warnings=warnings))
    return results


def detect_oscillation(values: list[Decimal | None], *, min_reversals: int = 2) -> bool:
    """隣接値間の増減符号が min_reversals 回以上反転したら「非単調な振動」とみなす。

    1回の反転(単峰性=山または谷が1つ)は正常な最適化曲面として許容する。
    2回以上の反転は smc_bos の rr_target 論点Bで確認された過剰適合の型
    (「単発の最良値がその期間の価格経路にたまたま合致しただけ」の兆候)。
    """
    nums = [v for v in values if v is not None]
    if len(nums) < 3:
        return False
    diffs = [b - a for a, b in zip(nums, nums[1:])]
    signs = [1 if d > 0 else (-1 if d < 0 else 0) for d in diffs if d != 0]
    reversals = sum(1 for a, b in zip(signs, signs[1:]) if a != b)
    return reversals >= min_reversals


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_param_arg(raw: str) -> tuple[str, list[str]]:
    name, _, vals = raw.partition("=")
    if not name or not vals:
        raise ValueError(f"--param must be 'name=v1,v2,...', got {raw!r}")
    return name, vals.split(",")


def _print_results(title: str, results: list[SweepResult]) -> None:
    print(f"\n=== {title} ===")
    for r in results:
        pf_str = str(r.profit_factor) if r.profit_factor is not None else "None"
        warn = f"  ⚠年次警告{len(r.warnings)}件" if r.warnings else ""
        print(f"  {r.label:40s} trades={r.trades:5d} PF={pf_str:14s} "
              f"win_rate={r.win_rate}  max_dd={r.max_dd_tick_steps}{warn}")
        for w in r.warnings:
            print(f"      - {w}")
    if detect_oscillation([r.profit_factor for r in results]):
        print("  ⚠️ PFが非単調に振動(過剰適合の疑い)。単発の最良値を「採用」と"
              "推奨しない(CLAUDE.md 定石6)。年次分解・追加検証で確認すること。")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", required=True, help="Parquetヒストリカルデータのパス")
    parser.add_argument("--symbol", default="XAUUSD", choices=sorted(SYMBOLS))
    parser.add_argument("--tf", default="M5", choices=[t.value for t in Timeframe])
    parser.add_argument("--provider-class", required=True,
                        help="'module:ClassName' 形式("
                             "例: infers.strategies.atr_trend_scalp.provider:AtrTrendScalpProvider)")
    parser.add_argument("--strategy", required=True,
                        help="registry登録名(execution_factoryの取得元。infers.strategies.registry)")
    parser.add_argument("--ai-client", choices=["rule", "none"], default="none")
    parser.add_argument("--base", action="append", default=[],
                        help="'name=value'(繰り返し可)。スイープ対象外パラメータの固定値")
    parser.add_argument("--param", action="append", default=[],
                        help="'name=v1,v2,v3'(繰り返し可)。スイープ対象パラメータと値候補")
    parser.add_argument("--mode", choices=["ofat", "grid"], default="ofat",
                        help="ofat=各--paramを他をbase固定で個別に掃引(既定)。"
                             "grid=全--paramの直積(組み合わせ探索)")
    parser.add_argument("--golden-trades", type=int, help="ベースライン自己検証: 黄金トレード数")
    parser.add_argument("--golden-pf", type=str, help="ベースライン自己検証: 黄金PF(文字列完全一致)")
    parser.add_argument("--golden-max-dd", type=int,
                        help="ベースライン自己検証: 黄金最大DD(tick*steps)")
    parser.add_argument("--from", dest="period_from", metavar="ISO8601")
    parser.add_argument("--to", dest="period_to", metavar="ISO8601")
    parser.add_argument("--last", dest="period_last", metavar="PERIOD")
    parser.add_argument("--contract-size", default="100")
    args = parser.parse_args(argv)

    module_name, _, cls_name = args.provider_class.partition(":")
    if not module_name or not cls_name:
        print("error: --provider-class must be 'module:ClassName'", file=sys.stderr)
        return 2
    provider_cls = getattr(importlib.import_module(module_name), cls_name)

    from infers.strategies.registry import get_strategy
    execution_factory = get_strategy(args.strategy).build_execution

    spec = SYMBOLS[args.symbol]
    tf = Timeframe(args.tf)
    candles = load_history(args.data, tf=tf)
    if args.period_from or args.period_to or args.period_last:
        from datetime import datetime, timezone

        from infers.backtest.slicing import slice_candles
        start = (datetime.fromisoformat(args.period_from).replace(tzinfo=timezone.utc)
                if args.period_from else None)
        end = (datetime.fromisoformat(args.period_to).replace(tzinfo=timezone.utc)
              if args.period_to else None)
        candles = slice_candles(candles, start=start, end=end, last=args.period_last)
        if not candles:
            print("error: --from/--to/--last でデータが0本になった", file=sys.stderr)
            return 2

    base: dict[str, str] = {}
    for raw in args.base:
        name, _, val = raw.partition("=")
        base[name] = val

    print("=== ベースライン自己検証 ===")
    try:
        report, _ = self_verify_baseline(
            candles, provider_cls=provider_cls, execution_factory=execution_factory,
            spec=spec, tf=tf, ai_client=args.ai_client,
            golden_trades=args.golden_trades, golden_pf=args.golden_pf,
            golden_max_dd=args.golden_max_dd, contract_size=args.contract_size)
    except SweepBaselineMismatch as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(f"OK: trades={len(report.trades)} PF={report.profit_factor} "
          f"max_dd={report.max_drawdown_tick_steps}")

    axes = [_parse_param_arg(p) for p in args.param]
    if not axes:
        print("\n--param 未指定のためベースライン自己検証のみで終了")
        return 0

    if args.mode == "ofat":
        for name, values in axes:
            configs = [SweepConfig(label=f"{name}={v}", overrides={**base, name: v})
                      for v in values]
            results = sweep(candles, provider_cls=provider_cls,
                            execution_factory=execution_factory, spec=spec, tf=tf,
                            configs=configs, ai_client=args.ai_client,
                            contract_size=args.contract_size)
            _print_results(f"OFAT: {name}", results)
    else:
        names = [n for n, _ in axes]
        value_lists = [v for _, v in axes]
        configs = []
        for combo in itertools.product(*value_lists):
            overrides = {**base, **dict(zip(names, combo))}
            label = ",".join(f"{n}={v}" for n, v in zip(names, combo))
            configs.append(SweepConfig(label=label, overrides=overrides))
        results = sweep(candles, provider_cls=provider_cls,
                        execution_factory=execution_factory, spec=spec, tf=tf,
                        configs=configs, ai_client=args.ai_client,
                        contract_size=args.contract_size)
        _print_results("GRID", results)

    return 0


if __name__ == "__main__":
    sys.exit(main())

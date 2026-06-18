"""バックテストHTMLレポート — 人間によるチェック用の可視化 (設計書 §8 補助)。

目的: バックテストの妥当性をトレーダーが目視検証できるようにする。
  ① 資金ベースのサマリー (初期資金→最終資金、USD損益、月次損益表、資産曲線)
  ② TradingView 系チャート (Lightweight Charts) でローソク足上に
     エントリー/退出マーカー・SL/無効化/第1波高値/フィボ目標の根拠ラインを描画

構成:
  - RecordingGateway : AiGateway をラップし、プランごとの最終 Verdict を記録
  - BacktestRecorder : engine.run(recorder=...) フックでトレード詳細を収集
  - write_html_report: report.html + report_data.js を出力 (ブラウザで開くだけ)

数値規約 (CLAUDE.md 第6条): 内部は整数ティック/Decimal のまま集計し、
float への変換は JS 表示境界のみ。チャート時刻は UTC unix 秒 (第7条)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Iterable

from infers.ai.gateway import AiGateway, JudgementRequest, Verdict
from infers.backtest.engine import BacktestReport, TradeRecord, _Ledger
from infers.core.loop import TradePlan
from infers.data.models import Candle, SymbolSpec, Timeframe

_Q2 = Decimal("0.01")


# 退出イベント → 表示種別。台帳 exits と同順で並ぶ (各退出が1件のジャーナル
# イベントに対応する)。"TP"=半分利確, "BE_SL"=建値SL(建値移動後のSL), "SL"=損切り,
# 残玉決済 CLOSE_ALL は reason で細分: "FIB"=フィボ目標到達, "DOW"=ダウ転換,
# "EOD"=データ末尾手仕舞い, "CLOSE"=その他。
_EXIT_EVENTS = {"HALF_TAKE_PROFIT", "SL_HIT", "CLOSE_ALL"}
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


def _request_key(request: JudgementRequest) -> str:
    """プラン⇔Verdict の照合キー (tier 非依存・決定論)。"""
    return json.dumps(
        {"symbol": request.symbol, "direction": request.direction,
         "features": request.features},
        sort_keys=True, default=str, ensure_ascii=False)


def _unix(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp())


class RecordingGateway:
    """AiGateway のラッパ: TradingLoop が使う judge/new_day を委譲しつつ、
    リクエスト→最終Verdict の対応を記録する (レポートの根拠表示用)。"""

    def __init__(self, inner: AiGateway) -> None:
        self._inner = inner
        self.verdicts: dict[str, Verdict] = {}

    def judge(self, request: JudgementRequest, *, cluster_score: Decimal,
              ambiguity: Decimal) -> Verdict:
        verdict = self._inner.judge(request, cluster_score=cluster_score,
                                    ambiguity=ambiguity)
        self.verdicts[_request_key(request)] = verdict
        return verdict

    def new_day(self) -> None:
        self._inner.new_day()

    @property
    def stats(self):
        return self._inner.stats

    @property
    def guardrail_reasons(self):
        return self._inner.guardrail_reasons

    def lookup(self, request: JudgementRequest) -> Verdict | None:
        return self.verdicts.get(_request_key(request))


@dataclass
class BacktestRecorder:
    """engine.run(recorder=...) フック実装。トレード/未約定プランの詳細を
    JSON化可能な dict として収集する。"""

    gateway: RecordingGateway | None = None
    trades: list[dict] = field(default_factory=list)
    unfilled: list[dict] = field(default_factory=list)

    def _verdict_dict(self, plan: TradePlan) -> dict | None:
        if self.gateway is None:
            return None
        v = self.gateway.lookup(plan.request)
        if v is None:
            return None
        return {"decision": v.decision, "confidence": str(v.confidence),
                "reasons": list(v.reasons), "source": v.source}

    @staticmethod
    def _plan_dict(plan: TradePlan) -> dict:
        return {
            "limit": plan.limit_price_int,
            "sl": plan.sl_int,
            "invalidation": plan.invalidation_price,
            "w1_high": plan.w1_high_int,
            "w1_low": plan.w1_low_int,
            "fib_target": plan.fib_target_int,
            # 選択トレードの根拠ライン描画用 (設計書 §3-4: FIB押し戻し / SRゾーン)
            "fib_levels": list(plan.fib_levels),
            "sr_zones": [list(z) for z in plan.sr_zones],
            "volume_steps": plan.volume_steps,
            "add_volume_steps": plan.add_volume_steps,
            "expiry": _unix(plan.expiry),
        }

    def on_trade_closed(self, record: TradeRecord, ledger: _Ledger,
                        fsm, plan: TradePlan) -> None:
        journal = [[name, payload] for name, payload in fsm.journal]
        self.trades.append({
            "id": record.position_id,
            "dir": record.direction,
            "pnl_ts": record.pnl_tick_steps,
            "swap_ts": record.swap_tick_steps,
            "exit_kind": record.exit_kind,
            "entries": [[_unix(t), p, v] for (p, v), t
                        in zip(ledger.entries, ledger.entry_times)],
            "exits": [[_unix(t), p, v] for (p, v), t
                      in zip(ledger.exits, ledger.exit_times)],
            # 各退出を1件ずつ分類 (台帳の exits と同順)。一律 exit_kind では
            # 半分利確まで "SL" と誤表示されるため (表示バグ修正)。
            "exit_kinds": classify_exits(journal),
            "journal": journal,
            "plan": self._plan_dict(plan),
            "features": dict(plan.request.features),
            "verdict": self._verdict_dict(plan),
        })

    def on_unfilled(self, position_id: str, fsm, plan: TradePlan) -> None:
        reason = ""
        for name, payload in reversed(fsm.journal):
            if name in ("CANCEL_PROBE", "ABORT_PENDING"):
                if payload.get("expired"):
                    reason = "expired"
                elif payload.get("invalidated"):
                    reason = "invalidated"
                else:
                    reason = str(payload.get("reason", "cancelled"))
                break
        self.unfilled.append({
            "id": position_id,
            "dir": plan.direction,
            "cancel_reason": reason,
            "plan": self._plan_dict(plan),
            "features": dict(plan.request.features),
            "verdict": self._verdict_dict(plan),
        })


# ---------------------------------------------------------------------------
# データ組み立て (Python側で確定計算し、JSは表示のみ)
# ---------------------------------------------------------------------------

def _money(ts: int | Decimal, usd_per_ts: Decimal) -> str:
    return str((Decimal(ts) * usd_per_ts).quantize(_Q2, rounding=ROUND_HALF_EVEN))


def _candles_columnar(candles: list[Candle]) -> dict:
    """時間は先頭値+差分 (ほぼ定数) で圧縮。価格は整数ティックのまま。"""
    ts = [_unix(c.open_time) for c in candles]
    dt = [ts[0]] + [b - a for a, b in zip(ts, ts[1:])]
    return {
        "t_delta": dt,
        "o": [c.o_int for c in candles],
        "h": [c.h_int for c in candles],
        "l": [c.l_int for c in candles],
        "c": [c.c_int for c in candles],
    }


def build_report_data(*, candles: list[Candle], report: BacktestReport,
                      recorder: BacktestRecorder, spec: SymbolSpec, tf: Timeframe,
                      initial_capital: Decimal, contract_size: Decimal,
                      jpy_rate: Decimal = Decimal(150)) -> dict:
    """report_data.js に埋め込む決定論 JSON を組み立てる。"""
    usd_per_ts = spec.tick_size * spec.lot_step * contract_size

    trades_out: list[dict] = []
    equity_usd: list[list] = []           # [unix, equity] (トレード確定ごと)
    monthly: dict[str, Decimal] = {}
    cum = Decimal(0)
    for t in recorder.trades:
        pnl_usd = Decimal(t["pnl_ts"]) * usd_per_ts
        cum += pnl_usd
        exit_t = t["exits"][-1][0] if t["exits"] else None
        entry_t = t["entries"][0][0] if t["entries"] else None
        if exit_t is not None:
            equity_usd.append([exit_t, str((initial_capital + cum).quantize(_Q2))])
            mkey = datetime.fromtimestamp(exit_t, tz=timezone.utc).strftime("%Y-%m")
            monthly[mkey] = monthly.get(mkey, Decimal(0)) + pnl_usd
        risk_ts = abs(t["plan"]["limit"] - t["plan"]["sl"]) * t["plan"]["volume_steps"]
        r_mult = (Decimal(t["pnl_ts"]) / risk_ts).quantize(_Q2) if risk_ts else None
        trades_out.append(dict(
            t,
            pnl_usd=str(pnl_usd.quantize(_Q2)),
            r_multiple=str(r_mult) if r_mult is not None else None,
            entry_time=entry_t,
            exit_time=exit_t,
        ))

    # 同一タイムスタンプ (同一バーで複数トレードが決済) を排除し、最後の累積値を
    # 採用する。Lightweight Charts の setData は厳密昇順・一意の time を要求するため、
    # 重複が残ると資産曲線が例外で一切描画されない (⑦ の不具合の根本原因)。
    if equity_usd:
        _eq_last: dict[int, str] = {}
        for _ts, _val in equity_usd:
            _eq_last[_ts] = _val
        equity_usd = [[k, _eq_last[k]] for k in sorted(_eq_last)]

    total_usd = Decimal(report.total_pnl_tick_steps) * usd_per_ts
    max_dd_usd = Decimal(report.max_drawdown_tick_steps) * usd_per_ts
    swap_ts_total = sum(t.get("swap_ts", 0) for t in recorder.trades)
    swap_usd = Decimal(swap_ts_total) * usd_per_ts
    gross_usd = total_usd - swap_usd          # スワップ計上前の粗利
    wins = [Decimal(t["pnl_ts"]) * usd_per_ts for t in recorder.trades
            if t["pnl_ts"] > 0]
    losses = [Decimal(t["pnl_ts"]) * usd_per_ts for t in recorder.trades
              if t["pnl_ts"] < 0]
    period_start = candles[0].open_time if candles else None
    period_end = candles[-1].close_time if candles else None
    years = (Decimal((period_end - period_start).days) / Decimal("365.25")
             if period_start and period_end else None)

    summary = {
        "symbol": spec.name,
        "tf": tf.value,
        "tick_size": str(spec.tick_size),
        "lot_step": str(spec.lot_step),
        "contract_size": str(contract_size),
        "usd_per_tick_step": str(usd_per_ts),
        "jpy_rate": str(jpy_rate),
        "period_start": _unix(period_start) if period_start else None,
        "period_end": _unix(period_end) if period_end else None,
        "bars": len(candles),
        "initial_capital": str(initial_capital.quantize(_Q2)),
        "final_equity": str((initial_capital + total_usd).quantize(_Q2)),
        "net_profit_usd": str(total_usd.quantize(_Q2)),
        "gross_profit_usd": str(gross_usd.quantize(_Q2)),     # スワップ前
        "swap_cost_usd": str(swap_usd.quantize(_Q2)),         # スワップ合計 (負=コスト)
        "return_pct": str((total_usd / initial_capital * 100).quantize(_Q2))
                      if initial_capital else None,
        "annual_profit_usd": str((total_usd / years).quantize(_Q2))
                             if years and years > 0 else None,
        "profit_factor": str(report.profit_factor) if report.profit_factor else None,
        "win_rate_pct": str((report.win_rate * 100).quantize(_Q2)),
        "be_sl_exit_rate_pct": str((report.be_sl_exit_rate * 100).quantize(_Q2)),
        "max_drawdown_usd": str(max_dd_usd.quantize(_Q2)),
        "max_drawdown_pct_of_capital":
            str((max_dd_usd / initial_capital * 100).quantize(_Q2))
            if initial_capital else None,
        "trades": len(recorder.trades),
        "unfilled_plans": len(recorder.unfilled),
        "avg_win_usd": str((sum(wins) / len(wins)).quantize(_Q2)) if wins else None,
        "avg_loss_usd": str((sum(losses) / len(losses)).quantize(_Q2)) if losses else None,
        "largest_win_usd": str(max(wins).quantize(_Q2)) if wins else None,
        "largest_loss_usd": str(min(losses).quantize(_Q2)) if losses else None,
        "volume_note": f"打診 {trades_out[0]['plan']['volume_steps'] if trades_out else 2}"
                       f" steps × lot_step {spec.lot_step} lot",
    }

    return {
        "summary": summary,
        "equity": equity_usd,
        "monthly": {k: str(v.quantize(_Q2)) for k, v in sorted(monthly.items())},
        "trades": trades_out,
        "unfilled": recorder.unfilled,
        "candles": _candles_columnar(candles),
        "bar_seconds": int(tf.duration.total_seconds()),
    }


def write_html_report(out_dir: str | Path, *, candles: list[Candle],
                      report: BacktestReport, recorder: BacktestRecorder,
                      spec: SymbolSpec, tf: Timeframe,
                      initial_capital: Decimal = Decimal(10_000),
                      contract_size: Decimal = Decimal(100),
                      jpy_rate: Decimal = Decimal(150)) -> Path:
    """report.html + report_data.js を out_dir に書き出し、html パスを返す。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    data = build_report_data(
        candles=candles, report=report, recorder=recorder, spec=spec, tf=tf,
        initial_capital=initial_capital, contract_size=contract_size,
        jpy_rate=jpy_rate)
    data_js = out / "report_data.js"
    data_js.write_text(
        "window.BT = " + json.dumps(data, sort_keys=True, ensure_ascii=False)
        + ";\n", encoding="utf-8")
    html = out / "report.html"
    html.write_text(_HTML_TEMPLATE, encoding="utf-8")
    return html


# ---------------------------------------------------------------------------
# HTML テンプレート (Lightweight Charts CDN。data は report_data.js から読む)
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>INFERS バックテストレポート</title>
<script src="https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script>
<script src="./report_data.js"></script>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #131722; color: #d1d4dc;
         font-family: "Segoe UI", Meiryo, sans-serif; font-size: 13px; }
  h1 { font-size: 18px; margin: 0; }
  h2 { font-size: 14px; margin: 0 0 8px; color: #9aa0ab; font-weight: 600; }
  .wrap { padding: 16px; max-width: 1900px; margin: 0 auto; }
  header { display: flex; justify-content: space-between; align-items: baseline;
           margin-bottom: 12px; }
  header .sub { color: #787b86; font-size: 12px; }
  /* ① バックテスト結果: 横1行のコンパクトな指標バー (溢れたら横スクロール) */
  .statbar { display: flex; gap: 6px; margin-bottom: 14px; overflow-x: auto;
             padding-bottom: 4px; }
  .statbar .card { flex: 0 0 auto; background: #1e222d; border: 1px solid #2a2e39;
                   border-radius: 5px; padding: 5px 9px; min-width: 96px; }
  .statbar .card .k { color: #787b86; font-size: 10px; margin-bottom: 2px;
                      white-space: nowrap; }
  .statbar .card .v { font-size: 14px; font-weight: 600; white-space: nowrap; }
  .pos { color: #26a69a; } .neg { color: #ef5350; }
  .row2 { display: grid; grid-template-columns: 2fr 1fr; gap: 12px; margin-bottom: 16px; }
  .panel { background: #1e222d; border: 1px solid #2a2e39; border-radius: 6px;
           padding: 12px; }
  #equity { height: 240px; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td { padding: 4px 8px; text-align: right; border-bottom: 1px solid #2a2e39;
           white-space: nowrap; }
  th { color: #787b86; position: sticky; top: 0; background: #1e222d; cursor: default; }
  td:first-child, th:first-child { text-align: left; }
  #monthly td.m { min-width: 56px; }
  .chartbox { margin-bottom: 16px; }
  /* ② チャートを大きく */
  #chart { height: 760px; }
  #rsi { height: 160px; border-top: 1px solid #2a2e39; position: relative; }
  /* ⑤ RSI 値ツールチップ */
  .tip { position: absolute; z-index: 6; pointer-events: none; display: none;
         background: #1e222d; border: 1px solid #4a4f5c; border-radius: 4px;
         padding: 3px 7px; font-size: 11px; color: #d1d4dc; white-space: nowrap; }
  .toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 8px;
             flex-wrap: wrap; }
  .toolbar button { background: #2a2e39; color: #d1d4dc; border: 1px solid #363a45;
                    border-radius: 4px; padding: 4px 10px; cursor: pointer; }
  .toolbar button:hover { background: #363a45; }
  .toolbar label { color: #9aa0ab; }
  #tf-btns button { border-radius: 0; margin: 0; border-left: none; }
  #tf-btns button:first-child { border-radius: 4px 0 0 4px; border-left: 1px solid #363a45; }
  #tf-btns button:last-child { border-radius: 0 4px 4px 0; }
  #tf-btns button.on { background: #2962ff; border-color: #2962ff; color: #fff; }
  /* ⑥ フィルタ */
  .filters { display: flex; gap: 8px; align-items: center; margin-bottom: 8px;
             flex-wrap: wrap; color: #9aa0ab; font-size: 12px; }
  .filters select, .filters input { background: #2a2e39; color: #d1d4dc;
             border: 1px solid #363a45; border-radius: 4px; padding: 3px 6px; }
  .filters button { background: #2a2e39; color: #d1d4dc; border: 1px solid #363a45;
             border-radius: 4px; padding: 3px 9px; cursor: pointer; }
  #tradetbl-box { max-height: 320px; overflow: auto; }
  #tradetbl tbody tr { cursor: pointer; }
  #tradetbl tbody tr:hover { background: #262b38; }
  #tradetbl tbody tr.sel { background: #2d3554; }
  #tradetbl tfoot td { position: sticky; bottom: 0; background: #20242f;
             font-weight: 600; border-top: 2px solid #363a45; }
  .grid2 { display: grid; grid-template-columns: 3fr 2fr; gap: 12px; margin-bottom: 16px; }
  #detail .kv { display: grid; grid-template-columns: 130px 1fr; gap: 2px 10px;
                font-size: 12px; }
  #detail .kv .k { color: #787b86; }
  #detail ul { margin: 4px 0; padding-left: 18px; }
  #detail .jr { color: #9aa0ab; font-size: 11px; }
  .legend { display: flex; gap: 14px; color: #9aa0ab; font-size: 11px;
            flex-wrap: wrap; margin-top: 6px; }
  .sw { display: inline-block; width: 10px; height: 10px; border-radius: 2px;
        margin-right: 4px; vertical-align: -1px; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>INFERS バックテストレポート <span id="hdr-sym"></span></h1>
    <div class="sub" id="hdr-period"></div>
  </header>

  <!-- ① バックテスト結果 (横1行コンパクト) -->
  <div class="statbar" id="cards"></div>

  <!-- ② チャート (大きめ) -->
  <div class="panel chartbox">
    <div class="toolbar">
      <h2 style="margin:0">チャート (<span id="hdr-tf"></span>)</h2>
      <span id="tf-btns">
        <button data-tf="300">M5</button><button data-tf="900">M15</button
        ><button data-tf="3600">H1</button><button data-tf="14400">H4</button
        ><button data-tf="86400">D1</button>
      </span>
      <button id="btn-prev">◀ 前のトレード</button>
      <button id="btn-next">次のトレード ▶</button>
      <button id="btn-fit">全期間</button>
      <label><input type="checkbox" id="chk-unfilled"> 未約定プランも表示</label>
      <span id="sel-label" style="color:#9aa0ab"></span>
    </div>
    <div id="chart"></div>
    <div id="rsi"></div>
    <div class="legend">
      <span><span class="sw" style="background:#2962ff"></span>SMA90</span>
      <span><span class="sw" style="background:#f59e0b"></span>SMA200</span>
      <span><span class="sw" style="background:#26a69a"></span>エントリー (▲買/▼売) / 半分利確 (■)</span>
      <span><span class="sw" style="background:#ab47bc"></span>フィボ目標利確 (■)</span>
      <span><span class="sw" style="background:#42a5f5"></span>ダウ転換決済 (■)</span>
      <span><span class="sw" style="background:#ffb74d"></span>建値SL (●)</span>
      <span><span class="sw" style="background:#ef5350"></span>損切りSL (●)</span>
      <span><span class="sw" style="background:#26a69a"></span>指値発注 (◇) 〜 失効期限 (◇) ・有効期間=破線</span>
      <span><span class="sw" style="background:#ffd54f"></span>選択トレード (黄=ハイライト, 選択時は他トレード非表示)</span>
      <span><span class="sw" style="background:#d4af37"></span>FIB押し戻し (38.2/50/61.8/78.6)</span>
      <span><span class="sw" style="background:#4dd0e1"></span>SRゾーン (上下端)</span>
      <span>— 根拠ライン: 指値 / SL初期 / 無効化 / 第1波高値 / フィボ161.8%目標 / SMA90・200</span>
    </div>
  </div>

  <!-- ③ トレード一覧 + 詳細 -->
  <div class="grid2">
    <div class="panel">
      <h2>トレード一覧 (行クリックでチャートへ)</h2>
      <!-- ⑥ フィルタ -->
      <div class="filters" id="filters">
        <span>方向</span>
        <select id="f-dir"><option value="">全</option><option value="1">買</option
          ><option value="-1">売</option></select>
        <span>退出</span>
        <select id="f-exit"><option value="">全</option><option value="TP">半分利確</option
          ><option value="BE_SL">建値SL</option><option value="SL">損切りSL</option
          ><option value="FIB">フィボ目標利確</option><option value="DOW">ダウ転換決済</option
          ><option value="EOD">期末手仕舞い</option><option value="CLOSE">手仕舞い</option></select>
        <span>勝敗</span>
        <select id="f-res"><option value="">全</option><option value="win">勝ち</option
          ><option value="loss">負け</option></select>
        <span>根拠</span>
        <input id="f-fam" placeholder="例 DOW / SMA / RSI" style="width:120px">
        <button id="f-clear">クリア</button>
      </div>
      <div id="tradetbl-box">
        <table id="tradetbl">
          <thead><tr>
            <th>#</th><th>エントリー日時(UTC)</th><th>方向</th><th>建値</th>
            <th>退出</th><th>退出種別</th><th>PnL</th><th>R</th>
            <th>根拠</th><th>conf</th>
          </tr></thead>
          <tbody></tbody>
          <tfoot id="tfoot"></tfoot>
        </table>
      </div>
    </div>
    <div class="panel" id="detail"><h2>トレード詳細</h2>
      <div id="detail-body" style="color:#787b86">トレードを選択してください。</div>
    </div>
  </div>

  <!-- ④ 資産曲線 + 月次損益 -->
  <div class="row2">
    <div class="panel"><h2>資産曲線</h2><div id="equity"></div></div>
    <div class="panel"><h2>月次損益</h2>
      <div style="max-height:240px;overflow:auto"><table id="monthly"></table></div>
    </div>
  </div>
</div>

<script>
"use strict";
const BT = window.BT;
const TICK = parseFloat(BT.summary.tick_size);
const BAR = BT.bar_seconds;
const px = v => v * TICK;
// 円表記: USD建ての *_usd を JPY へ換算して表示 (レートはUIで変更可)
let JPY = parseFloat(BT.summary.jpy_rate || "150");
const yen = usd => Math.round(parseFloat(usd) * JPY);
const fmtY = usd => (parseFloat(usd) >= 0 ? "+" : "") + "¥" +
  yen(usd).toLocaleString("ja-JP");
const fmtYp = usd => "¥" + yen(usd).toLocaleString("ja-JP");
const fmtD = t => new Date(t * 1000).toISOString().replace("T", " ").slice(0, 16);

// ---- ベース(M5)ローソクの復元 (t_delta は先頭値+差分) ----
const N = BT.candles.o.length;
const T = new Array(N);
{ let acc = 0; for (let i = 0; i < N; i++) { acc += BT.candles.t_delta[i]; T[i] = acc; } }

// ---- 上位TFへの集約 (M5 → M15/H1/H4/D1)。境界は tfSec で床関数 ----
const TF_DEFS = [
  ["M5", BAR], ["M15", 900], ["H1", 3600], ["H4", 14400], ["D1", 86400],
];
function aggregate(tfSec) {
  // 返り値: {t:[], o:[], h:[], l:[], c:[]} (価格は整数ティックのまま)
  const t = [], o = [], h = [], l = [], c = [];
  let bucket = null;
  for (let i = 0; i < N; i++) {
    const b = Math.floor(T[i] / tfSec) * tfSec;
    if (b !== bucket) {
      t.push(b); o.push(BT.candles.o[i]); h.push(BT.candles.h[i]);
      l.push(BT.candles.l[i]); c.push(BT.candles.c[i]); bucket = b;
    } else {
      const j = t.length - 1;
      if (BT.candles.h[i] > h[j]) h[j] = BT.candles.h[i];
      if (BT.candles.l[i] < l[j]) l[j] = BT.candles.l[i];
      c[j] = BT.candles.c[i];
    }
  }
  return { t, o, h, l, c };
}

// ---- SMA を全長配列で計算 (バー index で参照できるよう null 埋め, 価格単位) ----
function smaArray(agg, period) {
  const cc = agg.c, out = new Array(cc.length).fill(null); let sum = 0;
  for (let i = 0; i < cc.length; i++) {
    sum += cc[i];
    if (i >= period) sum -= cc[i - period];
    if (i >= period - 1) out[i] = px(sum / period);
  }
  return out;
}
function seriesFromArray(agg, arr) {
  const o = [];
  for (let i = 0; i < arr.length; i++) if (arr[i] != null) o.push({ time: agg.t[i], value: arr[i] });
  return o;
}
// ---- Wilder RSI (表示TFの終値列から再計算) ----
function rsiFrom(agg, period) {
  const out = []; let ag = 0, al = 0; const cc = agg.c;
  for (let i = 1; i < cc.length; i++) {
    const d = cc[i] - cc[i - 1];
    const g = d > 0 ? d : 0, ls = d < 0 ? -d : 0;
    if (i <= period) { ag += g / period; al += ls / period; }
    else { ag = (ag * (period - 1) + g) / period; al = (al * (period - 1) + ls) / period; }
    if (i >= period) {
      out.push({ time: agg.t[i], value: al === 0 ? 100 : 100 - 100 / (1 + ag / al) });
    }
  }
  return out;
}
// ---- agg.t (昇順バケット) で time 以下の最大バー index を二分探索 ----
function barIndexAtTime(agg, time) {
  const t = agg.t; if (!t.length) return 0;
  if (time <= t[0]) return 0;
  if (time >= t[t.length - 1]) return t.length - 1;
  let lo = 0, hi = t.length - 1, ans = 0;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (t[m] <= time) { ans = m; lo = m + 1; } else hi = m - 1; }
  return ans;
}

// ---- ヘッダ + 円レート入力 ----
const S = BT.summary;
document.getElementById("hdr-sym").textContent = `— ${S.symbol} ${S.tf}`;
document.getElementById("hdr-tf").textContent = `${S.symbol} ${S.tf}`;
document.getElementById("hdr-period").innerHTML =
  `${fmtD(S.period_start)} 〜 ${fmtD(S.period_end)} UTC ・ ${S.bars.toLocaleString()} 本 ・ ` +
  `1 tick*step = $${S.usd_per_tick_step} (contract ${S.contract_size}) ・ ${S.volume_note}` +
  ` ・ USD/JPY <input id="jpy" type="number" value="${JPY}" step="0.5" ` +
  `style="width:64px;background:#2a2e39;color:#d1d4dc;border:1px solid #363a45;` +
  `border-radius:4px;padding:2px 4px">`;
const net = parseFloat(S.net_profit_usd);

// ---- 資産曲線 (円換算) ----
const eqChart = LightweightCharts.createChart(document.getElementById("equity"), {
  layout: { background: { color: "#1e222d" }, textColor: "#9aa0ab" },
  grid: { vertLines: { color: "#2a2e39" }, horzLines: { color: "#2a2e39" } },
  timeScale: { timeVisible: false }, height: 240, autoSize: true,
});
const eqSeries = eqChart.addAreaSeries({
  lineColor: "#26a69a", topColor: "rgba(38,166,154,.3)",
  bottomColor: "rgba(38,166,154,0)", lineWidth: 2,
  priceFormat: { type: "price", precision: 0, minMove: 1 },
});
let eqInitLine = null;

// ---- 金額表示の再描画 (円レート変更で再実行) ----
function renderMoney() {
  const cards = [
    ["初期資金", fmtYp(S.initial_capital), ""],
    ["最終資金", fmtYp(S.final_equity), net >= 0 ? "pos" : "neg"],
    ["純利益 (スワップ後)", fmtY(S.net_profit_usd) + ` (${S.return_pct}%)`, net >= 0 ? "pos" : "neg"],
    ["粗利 / スワップ", `${fmtY(S.gross_profit_usd ?? S.net_profit_usd)} / ${fmtY(S.swap_cost_usd ?? 0)}`,
       parseFloat(S.swap_cost_usd ?? 0) < 0 ? "neg" : ""],
    ["年平均", S.annual_profit_usd ? fmtY(S.annual_profit_usd) : "-", net >= 0 ? "pos" : "neg"],
    ["プロフィットファクター", S.profit_factor ?? "∞", parseFloat(S.profit_factor ?? 99) >= 1 ? "pos" : "neg"],
    ["勝率", S.win_rate_pct + "%", ""],
    ["最大ドローダウン", "-" + fmtYp(S.max_drawdown_usd) +
       ` (初期資金比 ${S.max_drawdown_pct_of_capital}%)`, "neg"],
    ["トレード数", S.trades.toLocaleString() + ` (未約定 ${S.unfilled_plans.toLocaleString()})`, ""],
    ["建値SL退出率", S.be_sl_exit_rate_pct + "%", ""],
    ["平均勝ち / 平均負け", `${fmtY(S.avg_win_usd ?? 0)} / ${fmtY(S.avg_loss_usd ?? 0)}`, ""],
    ["最大勝ち / 最大負け", `${fmtY(S.largest_win_usd ?? 0)} / ${fmtY(S.largest_loss_usd ?? 0)}`, ""],
  ];
  document.getElementById("cards").innerHTML = cards.map(([k, v, cls]) =>
    `<div class="card"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`).join("");

  // ⑦ 同一タイムスタンプの重複を排除 (LWC は厳密昇順・一意の time を要求。重複が
  //    残ると setData が例外で資産曲線が一切描画されない)。最後の累積値を採用。
  const eqMap = new Map();
  for (const [t, v] of BT.equity) eqMap.set(t, yen(v));
  const eqData = [...eqMap.entries()].sort((a, b) => a[0] - b[0])
    .map(([time, value]) => ({ time, value }));
  eqSeries.setData(eqData);
  if (eqInitLine) eqSeries.removePriceLine(eqInitLine);
  eqInitLine = eqSeries.createPriceLine({ price: yen(S.initial_capital), color: "#787b86",
    lineStyle: LightweightCharts.LineStyle.Dashed, title: "初期資金" });
  eqChart.timeScale().fitContent();
  renderMonthly();
  if (typeof renderTradeTable === "function") renderTradeTable();
}

// ---- 月次損益表 (年 × 月, 円換算) ----
function renderMonthly() {
  const months = BT.monthly; const byYear = {};
  for (const [ym, v] of Object.entries(months)) {
    const [y, m] = ym.split("-"); (byYear[y] ??= {})[parseInt(m)] = parseFloat(v);
  }
  let html = "<thead><tr><th>年</th>" +
    [...Array(12)].map((_, i) => `<th class="m">${i + 1}月</th>`).join("") +
    "<th>合計</th></tr></thead><tbody>";
  for (const y of Object.keys(byYear).sort()) {
    let tot = 0;
    html += `<tr><td>${y}</td>` + [...Array(12)].map((_, i) => {
      const v = byYear[y][i + 1];
      if (v === undefined) return "<td class='m'>-</td>";
      tot += v;
      return `<td class="m ${v >= 0 ? "pos" : "neg"}">${fmtY(v)}</td>`;
    }).join("") + `<td class="${tot >= 0 ? "pos" : "neg"}">${fmtY(tot)}</td></tr>`;
  }
  document.getElementById("monthly").innerHTML = html + "</tbody>";
}

// ---- メインチャート + RSIペイン ----
const chartOpts = {
  layout: { background: { color: "#1e222d" }, textColor: "#9aa0ab" },
  grid: { vertLines: { color: "#2a2e39" }, horzLines: { color: "#2a2e39" } },
  timeScale: { timeVisible: true, secondsVisible: false },
  crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  autoSize: true,
};
const chart = LightweightCharts.createChart(document.getElementById("chart"), chartOpts);
const rsiChart = LightweightCharts.createChart(document.getElementById("rsi"), chartOpts);
const candleSeries = chart.addCandlestickSeries({
  upColor: "#26a69a", downColor: "#ef5350", wickUpColor: "#26a69a",
  wickDownColor: "#ef5350", borderVisible: false,
  priceFormat: { type: "price", precision: 2, minMove: TICK },
});
const sma90Series = chart.addLineSeries({ color: "#2962ff", lineWidth: 1,
  priceLineVisible: false, lastValueVisible: false });
const sma200Series = chart.addLineSeries({ color: "#f59e0b", lineWidth: 1,
  priceLineVisible: false, lastValueVisible: false });
const rsiLine = rsiChart.addLineSeries({ color: "#b39ddb", lineWidth: 1,
  priceLineVisible: false, lastValueVisible: false });
rsiLine.createPriceLine({ price: 70, color: "#787b86",
  lineStyle: LightweightCharts.LineStyle.Dashed, title: "70" });
rsiLine.createPriceLine({ price: 30, color: "#787b86",
  lineStyle: LightweightCharts.LineStyle.Dashed, title: "30" });

// ---- 表示TFの状態 (集約・SMA配列をグローバル保持: ズーム/グランビル計算で参照) ----
let curTF = BAR;
let curAgg = null, curSma90 = null, curSma200 = null;
function applyTF(tfSec) {
  curTF = tfSec;
  const agg = aggregate(tfSec); curAgg = agg;
  candleSeries.setData(agg.t.map((tt, i) => ({ time: tt, open: px(agg.o[i]),
    high: px(agg.h[i]), low: px(agg.l[i]), close: px(agg.c[i]) })));
  curSma90 = smaArray(agg, 90); curSma200 = smaArray(agg, 200);
  sma90Series.setData(seriesFromArray(agg, curSma90));
  sma200Series.setData(seriesFromArray(agg, curSma200));
  rsiLine.setData(rsiFrom(agg, 14));
  const lbl = (TF_DEFS.find(d => d[1] === tfSec) || ["?"])[0];
  document.querySelectorAll("#tf-btns button").forEach(b =>
    b.classList.toggle("on", parseInt(b.dataset.tf) === tfSec));
  document.getElementById("hdr-tf").textContent = `${S.symbol} ${lbl} (戦略はM5判定)`;
}
applyTF(BAR);

// 2チャートの時間軸同期
let syncing = false;
function syncFrom(src, dst) {
  src.timeScale().subscribeVisibleLogicalRangeChange(r => {
    if (syncing || r === null) return;
    syncing = true; dst.timeScale().setVisibleLogicalRange(r); syncing = false;
  });
}
syncFrom(chart, rsiChart); syncFrom(rsiChart, chart);

// ---- ⑤ RSI ツールチップ (マウスオーバーで RSI 値を表示) ----
const rsiBox = document.getElementById("rsi");
const rsiTip = document.createElement("div"); rsiTip.className = "tip"; rsiBox.appendChild(rsiTip);
rsiChart.subscribeCrosshairMove(param => {
  if (!param.time || !param.point) { rsiTip.style.display = "none"; return; }
  const sd = param.seriesData.get(rsiLine);
  const val = sd == null ? null : (typeof sd === "object" ? sd.value : sd);
  if (val == null) { rsiTip.style.display = "none"; return; }
  rsiTip.style.display = "block";
  rsiTip.innerHTML = `RSI <b>${val.toFixed(2)}</b> <span class="jr">${fmtD(param.time)}</span>`;
  const w = rsiBox.clientWidth;
  rsiTip.style.left = Math.min(param.point.x + 14, w - rsiTip.offsetWidth - 6) + "px";
  rsiTip.style.top = Math.max(4, param.point.y - 28) + "px";
});

// ---- ② エントリー↔退出 接続線 (選択トレード) ----
const linkSeries = chart.addLineSeries({ color: "#ffd54f", lineWidth: 2,
  lineStyle: LightweightCharts.LineStyle.Dotted, priceLineVisible: false,
  lastValueVisible: false, crosshairMarkerVisible: false });
// ---- ③ 指値の有効期間ライン (発注 → 失効を limit 価格水準で結ぶ破線) ----
const limitWinSeries = chart.addLineSeries({ color: "#26a69a", lineWidth: 2,
  lineStyle: LightweightCharts.LineStyle.Dashed, priceLineVisible: false,
  lastValueVisible: false, crosshairMarkerVisible: false });

// ---- 退出種別ごとの色・形・ラベル ----
const EXIT_META = {
  TP:    { label: "半分利確",     color: "#26a69a", shape: "square" },
  BE_SL: { label: "建値SL",       color: "#ffb74d", shape: "circle" },
  SL:    { label: "損切りSL",     color: "#ef5350", shape: "circle" },
  FIB:   { label: "フィボ目標利確", color: "#ab47bc", shape: "square" },
  DOW:   { label: "ダウ転換決済",  color: "#42a5f5", shape: "square" },
  EOD:   { label: "期末手仕舞い",  color: "#9aa0ab", shape: "square" },
  CLOSE: { label: "手仕舞い",     color: "#b2b5be", shape: "square" },
};
const EXIT_LABEL = k => (EXIT_META[k] || EXIT_META.SL).label;

// ---- ③ 指値の発注バー時刻 (id の ISO) と失効時刻 (plan.expiry) ----
function planTimes(t) {
  const seg = String(t.id).split("/");
  const iso = seg.slice(2).join("/");
  const place = Math.floor(Date.parse(iso) / 1000);
  return { place: Number.isFinite(place) ? place : null, exp: t.plan.expiry || null };
}
const snapTF = tm => Math.floor(tm / curTF) * curTF;

// ---- マーカー (全トレードのエントリー/退出 + 選択ハイライト + 未約定任意) ----
const trades = BT.trades;
let showUnfilled = false;
function buildMarkers(si) {
  const ms = [];
  trades.forEach((t, i) => {
    // 選択時は選択トレードのみ表示 (他は非表示)。未選択時は全件表示。
    if (si >= 0 && i !== si) return;
    const sel = i === si;
    for (const [tt, p, v] of t.entries) {
      ms.push({ time: tt, position: t.dir > 0 ? "belowBar" : "aboveBar",
        color: sel ? "#ffd54f" : (t.dir > 0 ? "#26a69a" : "#ef5350"),
        shape: t.dir > 0 ? "arrowUp" : "arrowDown", id: "T" + i,
        text: `#${i + 1} ${t.dir > 0 ? "買" : "売"} ${px(p).toFixed(2)}` });
    }
    t.exits.forEach(([tt, p, v], j) => {
      const k = (t.exit_kinds && t.exit_kinds[j]) || t.exit_kind;
      const meta = EXIT_META[k] || EXIT_META.SL;
      ms.push({ time: tt, position: t.dir > 0 ? "aboveBar" : "belowBar",
        color: sel ? "#ffd54f" : meta.color, shape: meta.shape,
        id: "T" + i, text: `#${i + 1} ${meta.label} ${px(p).toFixed(2)}` });
    });
  });
  // ③ 選択トレードの指値発注 / 失効期限マーカー
  if (si >= 0) {
    const t = trades[si], pt = planTimes(t);
    if (pt.place) ms.push({ time: snapTF(pt.place), position: "belowBar",
      color: "#26a69a", shape: "circle", text: `指値発注 ${px(t.plan.limit).toFixed(2)}` });
    if (pt.exp) ms.push({ time: snapTF(pt.exp), position: "aboveBar",
      color: "#ff8a65", shape: "circle", text: "指値失効期限" });
  }
  if (showUnfilled) {
    for (const u of BT.unfilled) {
      const t0 = Date.parse(u.id.split("/").slice(2).join("/")) / 1000;
      if (Number.isFinite(t0)) {
        ms.push({ time: t0, position: "inBar", color: "#5d606b", shape: "circle",
          text: `未約定 (${u.cancel_reason})` });
      }
    }
  }
  ms.sort((a, b) => a.time - b.time);
  candleSeries.setMarkers(ms);
}
buildMarkers(-1);
document.getElementById("chk-unfilled").addEventListener("change", e => {
  showUnfilled = e.target.checked; buildMarkers(selIdx);
});

// ---- ⑥ フィルタ ----
const F = {
  dir: document.getElementById("f-dir"), exit: document.getElementById("f-exit"),
  res: document.getElementById("f-res"), fam: document.getElementById("f-fam"),
};
function tradeMatches(t) {
  if (F.dir.value && String(t.dir) !== F.dir.value) return false;
  if (F.exit.value) {
    const lastk = (t.exit_kinds && t.exit_kinds.at(-1)) || t.exit_kind;
    if (lastk !== F.exit.value) return false;
  }
  const pnl = parseFloat(t.pnl_usd);
  if (F.res.value === "win" && !(pnl > 0)) return false;
  if (F.res.value === "loss" && !(pnl < 0)) return false;
  const fam = F.fam.value.trim().toUpperCase();
  if (fam && !String(t.features.families || "").toUpperCase().includes(fam)) return false;
  return true;
}

// ---- トレード一覧表 (円換算・フィルタ・合計行) ----
const tbody = document.querySelector("#tradetbl tbody");
function renderTradeTable() {
  let rows = "", n = 0, sum = 0, wins = 0, sumR = 0, nR = 0;
  trades.forEach((t, i) => {
    if (!tradeMatches(t)) return;
    n++; const pnl = parseFloat(t.pnl_usd); sum += pnl; if (pnl > 0) wins++;
    if (t.r_multiple != null) { sumR += parseFloat(t.r_multiple); nR++; }
    const v = t.verdict ?? {};
    rows += `<tr data-i="${i}" class="${i === selIdx ? "sel" : ""}"><td>${i + 1}</td>` +
      `<td>${t.entry_time ? fmtD(t.entry_time) : "-"}</td>` +
      `<td>${t.dir > 0 ? "買" : "売"}</td>` +
      `<td>${px(t.entries[0]?.[1] ?? 0).toFixed(2)}</td>` +
      `<td>${px(t.exits.at(-1)?.[1] ?? 0).toFixed(2)}</td>` +
      `<td>${EXIT_LABEL((t.exit_kinds && t.exit_kinds.at(-1)) || t.exit_kind)}</td>` +
      `<td class="${pnl >= 0 ? "pos" : "neg"}">${fmtY(t.pnl_usd)}</td>` +
      `<td>${t.r_multiple ?? "-"}</td>` +
      `<td style="text-align:left">${t.features.families}</td>` +
      `<td>${v.confidence ?? "-"}</td></tr>`;
  });
  tbody.innerHTML = rows;
  // フィルタ結果の合計 (件数 / 勝率 / 平均R / PnL合計)
  const wr = n ? (wins / n * 100).toFixed(1) : "0.0";
  const ar = nR ? (sumR / nR).toFixed(2) : "-";
  document.getElementById("tfoot").innerHTML =
    `<tr><td colspan="6">フィルタ結果 ${n} / ${trades.length} 件 ・ 勝率 ${wr}% ・ 平均R ${ar}</td>` +
    `<td class="${sum >= 0 ? "pos" : "neg"}">${fmtY(sum)}</td><td colspan="3"></td></tr>`;
}
[F.dir, F.exit, F.res].forEach(el => el.addEventListener("change", renderTradeTable));
F.fam.addEventListener("input", renderTradeTable);
document.getElementById("f-clear").onclick = () => {
  F.dir.value = ""; F.exit.value = ""; F.res.value = ""; F.fam.value = ""; renderTradeTable();
};

// ---- ④ グランビル近似分類 (チャート足の price/SMA/傾きから推定) ----
function granvilleClassify(dir, price, sma, slopeUp) {
  const above = price > sma;
  if (dir > 0) { // 買い ①〜④
    if (slopeUp && above) return { no: "買①", label: "SMA上向き×価格が上抜け (順張り初動)" };
    if (slopeUp && !above) return { no: "買②", label: "SMA上向き×価格が一時SMA割れ (押し目)" };
    if (!slopeUp && above) return { no: "買③", label: "SMA横ばい/失速×SMA上で浅い押し (反発)" };
    return { no: "買④", label: "SMA下向き×下方への大きな乖離 (自律反発狙い)" };
  } else { // 売り ⑤〜⑧
    if (!slopeUp && !above) return { no: "売⑤", label: "SMA下向き×価格が下抜け (順張り初動)" };
    if (!slopeUp && above) return { no: "売⑥", label: "SMA下向き×価格が一時SMA超え (戻り売り)" };
    if (slopeUp && !above) return { no: "売⑦", label: "SMA横ばい/失速×SMA下で浅い戻り (反落)" };
    return { no: "売⑧", label: "SMA上向き×上方への大きな乖離 (自律反落狙い)" };
  }
}
// family 短縮名 → 説明 (詳細パネルの可読性向上)
const FAM_DESC = { DOW: "ダウ順行", GRANVILLE: "グランビル", SMA: "SMAグランビル",
  RSI: "RSIマルチTF", SR: "水平レジサポ", FIB: "フィボ押し戻し", ELLIOTT: "エリオット第2波" };
function famPretty(fams) {
  return String(fams || "").split(",").filter(Boolean)
    .map(f => `${f}${FAM_DESC[f] ? `(${FAM_DESC[f]})` : ""}`).join(" + ");
}

// ---- トレード選択 (チャートズーム + 根拠ライン + 接続線 + ハイライト + 詳細) ----
let selIdx = -1;
let priceLines = [];
function clearLines() { priceLines.forEach(l => candleSeries.removePriceLine(l)); priceLines = []; }
function addLine(price, color, title, style) {
  priceLines.push(candleSeries.createPriceLine({ price: px(price), color, title,
    lineStyle: style ?? LightweightCharts.LineStyle.Dashed, lineWidth: 1 }));
}
function select(i) {
  if (i < 0 || i >= trades.length) return;
  selIdx = i;
  const t = trades[i];
  document.querySelectorAll("#tradetbl tbody tr").forEach(r =>
    r.classList.toggle("sel", parseInt(r.dataset.i) === i));
  document.getElementById("sel-label").textContent =
    `選択中: #${i + 1} (${t.id})  PnL ${fmtY(t.pnl_usd)}`;
  buildMarkers(i);
  // エントリー↔退出を点線で接続 (最初のエントリー → 最後の退出)。
  // 同一足で約定・決済した場合 (entry_time === exit_time) は2点が同時刻になり
  // LWC が例外を投げ、チャートが壊れるため、退出が後の場合のみ描画する。
  if (t.entry_time && t.exit_time && t.exit_time > t.entry_time) {
    const ep = px(t.entries[0][1]), xp = px(t.exits.at(-1)[1]);
    linkSeries.setData([{ time: t.entry_time, value: ep },
                        { time: t.exit_time, value: xp }]);
  } else { linkSeries.setData([]); }
  // ③ 指値の有効期間ライン (発注バー → 失効) を limit 価格で結ぶ
  const p = t.plan;
  const pt = planTimes(t);
  if (pt.place && pt.exp) {
    let a = snapTF(pt.place), b = snapTF(pt.exp); if (b <= a) b = a + curTF;
    limitWinSeries.setData([{ time: a, value: px(p.limit) }, { time: b, value: px(p.limit) }]);
  } else { limitWinSeries.setData([]); }
  // ② ズーム: エントリーを中心にバー数ベースで合わせる (TFに依らず一定の見やすさ)
  const eI = barIndexAtTime(curAgg, t.entry_time ?? t.exit_time);
  const xI = barIndexAtTime(curAgg, t.exit_time ?? t.entry_time);
  const tradeBars = Math.max(1, xI - eI);
  let win = Math.min(220, Math.max(60, tradeBars + 40));
  let center = eI;
  // 退出が窓に収まらない長期保有は中点中心に広げる (両端が見えるように)
  if (xI - eI > win * 0.6) { win = Math.min(280, Math.max(win, (xI - eI) + 30)); center = Math.round((eI + xI) / 2); }
  chart.timeScale().setVisibleLogicalRange({ from: center - win / 2, to: center + win / 2 });
  // 根拠ライン
  clearLines();
  addLine(p.limit, "#26a69a", "指値(合流点)");
  addLine(p.sl, "#ef5350", "SL初期", LightweightCharts.LineStyle.Solid);
  addLine(p.invalidation, "#ff6d00", "エリオット無効化");
  addLine(p.w1_high, "#2962ff", "第1波高値(追撃基準)");
  addLine(p.fib_target, "#ab47bc", "フィボ161.8%目標");
  // 選択トレードの根拠: FIB押し戻し水準 (38.2/50/61.8/78.6) と 近傍SRゾーン
  const FR = ["38.2%", "50%", "61.8%", "78.6%"];
  (p.fib_levels || []).forEach((lv, k) =>
    addLine(lv, "#d4af37", "Fib " + (FR[k] || ""), LightweightCharts.LineStyle.Dotted));
  (p.sr_zones || []).forEach(([lo, hi], k) => {
    addLine(lo, "#4dd0e1", "SR" + (k + 1) + "下端", LightweightCharts.LineStyle.Dashed);
    addLine(hi, "#4dd0e1", "SR" + (k + 1) + "上端", LightweightCharts.LineStyle.Dashed);
  });
  // 詳細パネル
  const v = t.verdict ?? {};
  const f = t.features;
  const dir = t.dir;
  const swHi = Math.max(p.w1_high, p.w1_low), swLo = Math.min(p.w1_high, p.w1_low);
  const swSpan = swHi - swLo;
  const depth = swSpan > 0 ? (dir > 0 ? (p.limit - swLo) : (swHi - p.limit)) / swSpan : 0;
  const depthOk = depth <= 0.40 ? "✓深い(40%以内)" : "△浅い(40%超)";
  const halfTrig = (t.journal.find(([n]) => n === "HALF_TAKE_PROFIT") || [null, {}])[1].trigger || "—";
  // ④ グランビル近似 + SMA乖離% (エントリー足のチャートSMAから算出)
  const eI2 = barIndexAtTime(curAgg, t.entry_time ?? t.exit_time);
  const sma90v = curSma90[eI2], sma200v = curSma200[eI2];
  const pricev = px(t.entries[0]?.[1] ?? curAgg.c[eI2]);
  let gv = null, dev90 = null, dev200 = null;
  if (sma90v != null) {
    const k = Math.max(1, Math.min(3, eI2));
    const slopeUp = sma90v - (curSma90[eI2 - k] ?? sma90v) >= 0;
    gv = granvilleClassify(dir, pricev, sma90v, slopeUp);
    dev90 = (pricev - sma90v) / sma90v * 100;
  }
  if (sma200v != null) dev200 = (pricev - sma200v) / sma200v * 100;
  const devTxt = `SMA90 ${dev90 != null ? (dev90 >= 0 ? "+" : "") + dev90.toFixed(2) + "%" : "—"}` +
    ` / SMA200 ${dev200 != null ? (dev200 >= 0 ? "+" : "") + dev200.toFixed(2) + "%" : "—"}`;
  const journey = t.journal.map(([n, pl]) => {
    const ps = Object.entries(pl).filter(([k]) => k !== "state")
      .map(([k, x]) => `${k}=${x}`).join(", ");
    return `<li><b>${n}</b> <span class="jr">${ps}</span></li>`;
  }).join("");
  document.getElementById("detail-body").innerHTML = `
    <div class="kv">
      <div class="k">ID</div><div>${t.id}</div>
      <div class="k">損益(スワップ後)</div><div class="${parseFloat(t.pnl_usd) >= 0 ? "pos" : "neg"}">
        ${fmtY(t.pnl_usd)} (${t.pnl_ts} tick*steps, R=${t.r_multiple ?? "-"}${
        t.swap_ts ? `, うちスワップ ${t.swap_ts} ts` : ""})</div>
      <div class="k">ゲート判定</div><div>${v.decision ?? "-"} (conf=${v.confidence ?? "-"},
        source=${v.source ?? "-"})</div>
      <div class="k">判定理由</div><div><ul>${(v.reasons ?? []).map(r => `<li>${r}</li>`).join("")}</ul></div>
      <div class="k">方向 / マクロ</div><div>${dir > 0 ? "買い" : "売り"} (ダウ ${f.dow_state}) /
        マクロダウ ${f.macro_dow ?? "-"} / 第2波TF ${f.wave2_tf ?? "M5"}</div>
      <div class="k">グランビル(近似)</div><div>${gv ? `<b>${gv.no}</b> ${gv.label}` : "—"}
        <span class="jr">(SMA90基準・チャート足から推定)</span></div>
      <div class="k">SMA乖離</div><div>${devTxt} <span class="jr">(エントリー足)</span></div>
      <div class="k">コンフルエンス</div><div>${famPretty(f.families)}
        <span class="jr">(cluster_score=${f.cluster_score})</span></div>
      <div class="k">根拠強度</div><div>ダウ ${f.dow_strength ?? "-"} / RSI ${f.rsi_strength ?? "-"}
        (上位足 順${f.rsi_mtf_aligned ?? "-"}/逆${f.rsi_mtf_conflict ?? "-"}) /
        SMA ${f.sma_strength ?? "-"} / SR ${f.sr_strength ?? "-"}</div>
      <div class="k">押し目/戻り深さ</div><div>${(depth * 100).toFixed(1)}% (基準=直近スイング高安) ${depthOk}</div>
      <div class="k">半分利確トリガー</div><div>${halfTrig} (RSI / SMA90 / SR のいずれか)</div>
      <div class="k">rsi / band</div><div>現在 ${f.rsi} → 到達予測 ${f.rsi_band} (eta ${f.eta_bars} 本)</div>
      <div class="k">指値の有効期限</div><div>発注 ${pt.place ? fmtD(snapTF(pt.place)) : "-"}
        → 失効 ${pt.exp ? fmtD(pt.exp) : "-"}</div>
      <div class="k">ambiguity</div><div>${f.ambiguity}</div>
      <div class="k">価格構造</div><div>limit ${px(t.plan.limit).toFixed(2)} /
        無効化 ${px(t.plan.invalidation).toFixed(2)} / W1 ${px(t.plan.w1_high).toFixed(2)} /
        目標 ${px(t.plan.fib_target).toFixed(2)}</div>
    </div>
    <h2 style="margin-top:10px">執行イベント</h2><ul>${journey}</ul>`;
}
tbody.addEventListener("click", e => {
  const tr = e.target.closest("tr"); if (tr) select(parseInt(tr.dataset.i));
});
document.getElementById("btn-prev").onclick = () => select(Math.max(0, selIdx - 1));
document.getElementById("btn-next").onclick = () =>
  select(Math.min(trades.length - 1, selIdx + 1));
document.getElementById("btn-fit").onclick = () => chart.timeScale().fitContent();

// 上位TF切替: 選択中はトレードに再フォーカス、未選択は表示範囲を維持
document.querySelectorAll("#tf-btns button").forEach(btn => {
  btn.onclick = () => {
    const vr = chart.timeScale().getVisibleRange();
    applyTF(parseInt(btn.dataset.tf));
    if (selIdx >= 0) { const i = selIdx; selIdx = -1; select(i); }
    else if (vr) chart.timeScale().setVisibleRange(vr);
  };
});

// マーカークリックでトレード選択
chart.subscribeClick(param => {
  if (param.hoveredObjectId && String(param.hoveredObjectId).startsWith("T")) {
    select(parseInt(String(param.hoveredObjectId).slice(1)));
  }
});

// 円レート変更 → 全金額を再描画
document.getElementById("jpy").addEventListener("input", e => {
  const r = parseFloat(e.target.value);
  if (Number.isFinite(r) && r > 0) { JPY = r; renderMoney(); }
});

// 初期描画
renderTradeTable();
renderMoney();
// 初期表示: 末尾500本
if (N > 500) chart.timeScale().setVisibleRange({ from: T[N - 500], to: T[N - 1] });
</script>
</body>
</html>
"""

"""ローカル監視ダッシュボードの HTTP サーバ (標準ライブラリのみ)。

127.0.0.1 のみ bind し、外部公開しない。POST 系操作はコンソール表示の
トークン (X-Infers-Token ヘッダ) を要求する最小限のガードを設ける。
資格情報は POST /api/start のボディで受領後、controller (factory) へ渡す
だけで応答・ログには一切返さない (CLAUDE.md 安全原則)。
"""

from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from infers.dashboard import monitor
from infers.dashboard.controller import LiveController


def make_handler(controller: LiveController, *, token: str, default_symbol: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "InfersDashboard/1.0"

        # -- ヘルパ ------------------------------------------------------
        def _send_json(self, obj, status: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _require_token(self) -> bool:
            if self.headers.get("X-Infers-Token") == token:
                return True
            self._send_json({"error": "invalid or missing token"}, status=401)
            return False

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        def _symbol(self) -> str:
            running = controller.status().get("symbol")
            if running:
                return running
            qs = parse_qs(urlparse(self.path).query)
            return (qs.get("symbol") or [default_symbol])[0]

        # サイレント化: アクセスログに資格情報パスは載らないが冗長なので抑制
        def log_message(self, fmt, *args):  # noqa: A002 - stdlib シグネチャ
            pass

        # -- ルーティング ------------------------------------------------
        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/":
                self._send_html(_INDEX_HTML)
            elif path == "/api/status":
                self._send_json(controller.status())
            elif path == "/api/journal":
                # 稼働中セッションの実ファイルを優先する。日付(UTC)で再計算すると
                # 起動日のファイルに書き続けるセッションと UTC 日付跨ぎでズレて
                # 監視が空表示になるため (起動層の日付ロールオーバーバグ修正)。
                jpath = (controller.status().get("journal_path")
                         or monitor.journal_path_for(self._symbol()))
                self._send_json(monitor.summarize(jpath))
            else:
                self._send_json({"error": "not found"}, status=404)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path not in ("/api/start", "/api/stop"):
                self._send_json({"error": "not found"}, status=404)
                return
            if not self._require_token():
                return
            if path == "/api/start":
                self._handle_start()
            else:
                self._handle_stop()

        def _handle_start(self) -> None:
            body = self._read_body()
            symbol = str(body.get("symbol") or default_symbol)
            login_raw = body.get("login")
            try:
                login = int(login_raw) if login_raw not in (None, "") else None
            except (TypeError, ValueError):
                self._send_json({"error": "login must be numeric"}, status=400)
                return
            password = body.get("password") or None
            server = body.get("server") or None
            try:
                warmup_days = int(body.get("warmup_days") or 0)
            except (TypeError, ValueError):
                warmup_days = 0
            try:
                controller.start(symbol=symbol, login=login,
                                 password=password, server=server,
                                 warmup_days=warmup_days)
            except RuntimeError as exc:
                self._send_json({"error": str(exc)}, status=409)
                return
            except Exception as exc:              # noqa: BLE001 — 接続失敗等を UI へ
                self._send_json({"error": repr(exc)}, status=500)
                return
            # 応答に資格情報は含めない
            self._send_json({"ok": True, "status": controller.status()})

        def _handle_stop(self) -> None:
            controller.stop()
            self._send_json({"ok": True, "status": controller.status()})

    return Handler


def make_server(*, port: int = 8765, default_symbol: str = "XAUUSD",
                token: str | None = None,
                controller: LiveController | None = None) -> tuple[ThreadingHTTPServer, str]:
    """127.0.0.1 のみで待ち受けるサーバとトークンを返す。"""
    controller = controller or LiveController()
    token = token or secrets.token_urlsafe(16)
    handler = make_handler(controller, token=token, default_symbol=default_symbol)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    return httpd, token


_INDEX_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>INFERS 監視ダッシュボード</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, sans-serif; background:#0e1117; color:#e6edf3;
         margin:0; padding:24px; }
  h1 { font-size:20px; margin:0 0 16px; }
  .grid { display:grid; grid-template-columns: 360px 1fr; gap:16px; align-items:start; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:16px; }
  label { display:block; font-size:12px; color:#8b949e; margin:10px 0 4px; }
  input, select { width:100%; box-sizing:border-box; padding:8px; background:#0d1117;
                  color:#e6edf3; border:1px solid #30363d; border-radius:6px; }
  button { margin-top:14px; padding:9px 14px; border:0; border-radius:6px;
           font-weight:600; cursor:pointer; }
  .start { background:#238636; color:#fff; }
  .stop { background:#da3633; color:#fff; margin-left:8px; }
  button:disabled { opacity:0.4; cursor:not-allowed; }
  .pill { display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; }
  .on { background:#238636; } .off { background:#6e7681; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th,td { text-align:left; padding:4px 6px; border-bottom:1px solid #21262d; }
  .muted { color:#8b949e; font-size:12px; }
  .err { color:#f85149; }
  .ok { color:#3fb950; }
  code { background:#0d1117; padding:1px 5px; border-radius:4px; }
</style>
</head>
<body>
<h1>INFERS 監視ダッシュボード <span class="muted">(v1.0 / rule_depth50 / デモ専用)</span></h1>
<div class="grid">
  <div class="card">
    <h3 style="margin-top:0">接続・起動</h3>
    <label>操作トークン (起動時コンソールに表示)</label>
    <input id="token" type="password" placeholder="X-Infers-Token">
    <label>銘柄</label>
    <select id="symbol"><option>XAUUSD</option><option>BTCUSD</option></select>
    <label>口座番号 (login)</label>
    <input id="login" inputmode="numeric" placeholder="例: 785662">
    <label>パスワード</label>
    <input id="password" type="password" placeholder="送信後フォームから消去されます">
    <label>サーバ</label>
    <input id="server" placeholder="例: VantageTradingLtd-Demo">
    <label>ウォームアップ日数 (0=無効。D1/H1 200SMA を育てるには 300 推奨)</label>
    <input id="warmup" inputmode="numeric" value="300">
    <div>
      <button id="btnStart" class="start" onclick="start()">監視開始</button>
      <button id="btnStop" class="stop" onclick="stop()">安全停止</button>
    </div>
    <p class="muted">構成は v1.0 固定 (--macro-wave2 --depth-screen --depth-max 0.50
      --no-fib-score)。資格情報はメモリ保持のみでディスクに保存しません。</p>
    <p id="msg" class="muted"></p>
  </div>
  <div>
    <div class="card">
      <h3 style="margin-top:0">稼働ステータス
        <span id="state" class="pill off">停止中</span></h3>
      <table id="status"></table>
    </div>
    <div class="card" style="margin-top:16px">
      <h3 style="margin-top:0">ジャーナル監視
        <span class="muted">(自動更新: <span id="interval">10</span>秒)</span></h3>
      <div id="golden" class="muted"></div>
      <div id="positions"></div>
      <h4>直近イベント</h4>
      <table id="events"><thead><tr><th>seq</th><th>kind</th><th>bar_time</th>
        <th>data</th></tr></thead><tbody></tbody></table>
    </div>
  </div>
</div>
<script>
// login/server/symbol はブラウザ localStorage に保存 (サーバ側ディスクには書かない)。
// パスワードは保存しない。
const PREFS='infers_dashboard_prefs';
function loadPrefs(){
  try {
    const p = JSON.parse(localStorage.getItem(PREFS)||'{}');
    if(p.login) document.getElementById('login').value = p.login;
    if(p.server) document.getElementById('server').value = p.server;
    if(p.symbol) document.getElementById('symbol').value = p.symbol;
    if(p.token) document.getElementById('token').value = p.token;
    if(p.warmup!=null) document.getElementById('warmup').value = p.warmup;
  } catch(e){}
}
function savePrefs(){
  const p = { login: document.getElementById('login').value,
    server: document.getElementById('server').value,
    symbol: document.getElementById('symbol').value,
    token: document.getElementById('token').value,
    warmup: document.getElementById('warmup').value };   // パスワードは保存しない
  localStorage.setItem(PREFS, JSON.stringify(p));
}
function tok(){ return document.getElementById('token').value.trim(); }
function msg(t, cls){ const m=document.getElementById('msg'); m.textContent=t;
  m.className = cls||'muted'; }
async function start(){
  const body = { symbol: document.getElementById('symbol').value,
    login: document.getElementById('login').value,
    password: document.getElementById('password').value,
    server: document.getElementById('server').value,
    warmup_days: document.getElementById('warmup').value };
  savePrefs();                                        // 入力値を保存 (PW除く)
  const r = await fetch('/api/start', { method:'POST',
    headers:{'Content-Type':'application/json','X-Infers-Token':tok()},
    body: JSON.stringify(body) });
  const j = await r.json();
  document.getElementById('password').value = '';   // 即時消去
  if(r.ok){ msg('監視を開始しました', 'ok'); } else { msg('開始失敗: '+(j.error||r.status), 'err'); }
  refresh();
}
async function stop(){
  const r = await fetch('/api/stop', { method:'POST',
    headers:{'X-Infers-Token':tok()} });
  const j = await r.json();
  if(r.ok){ msg('安全停止しました', 'ok'); } else { msg('停止失敗: '+(j.error||r.status), 'err'); }
  refresh();
}
async function refresh(){
  try {
    const s = await (await fetch('/api/status')).json();
    const st = document.getElementById('state');
    const phaseLabel = s.phase==='warmup' ? 'ウォームアップ中'
      : (s.phase==='live' ? '稼働中' : '停止中');
    st.textContent = s.running ? phaseLabel : '停止中';
    st.className = 'pill ' + (s.running ? 'on' : 'off');
    document.getElementById('btnStart').disabled = s.running;
    document.getElementById('btnStop').disabled = !s.running;
    const off = (s.server_utc_offset_h==null) ? '-' : ('UTC+'+s.server_utc_offset_h);
    const warm = (s.warmup_bars==null) ? '-' : (s.warmup_bars+' 本');
    const rows = [['銘柄', s.symbol||'-'], ['フェーズ', phaseLabel],
      ['開始(UTC)', s.started_at||'-'],
      ['ウォームアップ本数', warm],
      ['処理バー数(ライブ)', s.bars_processed], ['最終処理足(UTC)', s.last_bar_time||'-'],
      ['サーバ時刻オフセット', off],
      ['ジャーナル', s.journal_path||'-'], ['停止理由', s.stopped_reason||'-']];
    document.getElementById('status').innerHTML =
      rows.map(r=>`<tr><th>${r[0]}</th><td>${r[1]}</td></tr>`).join('') +
      (s.last_error ? `<tr><th>エラー</th><td class="err">${s.last_error}</td></tr>` : '');
  } catch(e){}
}
async function refreshJournal(){
  try {
    const j = await (await fetch('/api/journal')).json();
    const g = document.getElementById('golden');
    if(j.ai_client==='rule'){
      g.innerHTML = j.golden.ok
        ? `<span class="ok">ゴールデンリプレイ OK (${j.golden.checked}件一致・退行なし)</span>`
        : `<span class="err">退行検出: ${JSON.stringify(j.golden.mismatches)}</span>`;
    } else { g.textContent=''; }
    const pos = Object.entries(j.positions||{}).map(
      ([pid,tl])=>`<div><code>${pid}</code> : ${tl.join(' → ')}</div>`).join('');
    document.getElementById('positions').innerHTML =
      pos || '<span class="muted">ポジションなし</span>';
    const tb = document.querySelector('#events tbody');
    tb.innerHTML = (j.recent_events||[]).slice().reverse().map(e=>
      `<tr><td>${e.seq}</td><td>${e.kind}</td><td>${e.bar_time||''}</td>
       <td><code>${JSON.stringify(e.data).slice(0,160)}</code></td></tr>`).join('');
  } catch(e){}
}
loadPrefs();
setInterval(refresh, 2000);
setInterval(refreshJournal, 10000);
refresh(); refreshJournal();
</script>
</body>
</html>
"""

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

from infers.dashboard import accounts, monitor
from infers.dashboard.controller import LiveController


def make_handler(controller: LiveController, *, token: str, default_symbol: str,
                  account_specs: list[tuple[str, str | None]] | None = None):
    account_specs = account_specs or [("既定", None)]
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
            elif path == "/api/journals":
                # work/journal/ 配下の全ジャーナルを読み取り専用で列挙 (複数手法/複数
                # プロセスを別ターミナルでCLI稼働させている場合の横並び監視用。
                # このダッシュボード自身は新たなセッションを起動しない)。
                files = monitor.list_journal_files()
                self._send_json({
                    "files": [
                        {"name": f.name, **monitor.summarize(f)}
                        for f in files
                    ]
                })
            elif path == "/api/accounts":
                # 設定済みの全ターミナルへ読み取り専用で接続し、現在の口座情報
                # (login/server/balance/equity) を都度取得して返す。発注APIには
                # 触れない (accounts.account_snapshot 参照)。
                self._send_json({
                    "accounts": [
                        accounts.account_snapshot(p, label=label)
                        for label, p in account_specs
                    ]
                })
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
                controller: LiveController | None = None,
                account_specs: list[tuple[str, str | None]] | None = None,
                ) -> tuple[ThreadingHTTPServer, str]:
    """127.0.0.1 のみで待ち受けるサーバとトークンを返す。"""
    controller = controller or LiveController()
    token = token or secrets.token_urlsafe(16)
    handler = make_handler(controller, token=token, default_symbol=default_symbol,
                           account_specs=account_specs)
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
         margin:0; padding:24px; max-width:980px; }
  h1 { font-size:20px; margin:0 0 16px; }
  .card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:16px;
          margin-bottom:16px; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  th,td { text-align:left; padding:4px 6px; border-bottom:1px solid #21262d; }
  .muted { color:#8b949e; font-size:12px; }
  .err { color:#f85149; }
  .ok { color:#3fb950; }
  code { background:#0d1117; padding:1px 5px; border-radius:4px; }
  .acct-grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
               gap:12px; }
  .acct-card { background:#0d1117; border:1px solid #30363d; border-radius:6px; padding:10px; }
</style>
</head>
<body>
<h1>INFERS 監視ダッシュボード <span class="muted">(読み取り専用・トレード操作なし)</span></h1>
<div class="card">
  <h3 style="margin-top:0">現在接続中のMT5口座
    <span class="muted">(読み取り専用・自動更新: 10秒)</span></h3>
  <div id="accounts" class="muted">読み込み中...</div>
</div>
<div class="card">
  <h3 style="margin-top:0">全ジャーナル一覧 (読み取り専用・work/journal/*.jsonl)
    <span class="muted">(別ターミナルでCLI稼働中の手法も含む。自動更新: 10秒)</span></h3>
  <div id="allJournals" class="muted">読み込み中...</div>
</div>
<script>
function renderAccountBlock(a){
  if(!a.connected){
    return `<div class="acct-card"><strong>${a.label}</strong>
      <div class="err">未接続: ${a.error||'unknown'}</div></div>`;
  }
  return `<div class="acct-card"><strong>${a.label}</strong>
    <table>
      <tr><th>login</th><td>${a.login}</td></tr>
      <tr><th>server</th><td>${a.server}</td></tr>
      <tr><th>balance</th><td>${a.balance} ${a.currency}</td></tr>
      <tr><th>equity</th><td>${a.equity} ${a.currency}</td></tr>
      <tr><th>leverage</th><td>1:${a.leverage}</td></tr>
    </table></div>`;
}
async function refreshAccounts(){
  try {
    const a = await (await fetch('/api/accounts')).json();
    const el = document.getElementById('accounts');
    el.innerHTML = (a.accounts||[]).length
      ? `<div class="acct-grid">${a.accounts.map(renderAccountBlock).join('')}</div>`
      : '<span class="muted">設定済みターミナルがありません</span>';
  } catch(e){}
}
function renderJournalBlock(j){
  const golden = j.ai_client==='rule'
    ? (j.golden.ok
        ? `<span class="ok">ゴールデンリプレイ OK (${j.golden.checked}件一致)</span>`
        : `<span class="err">退行検出: ${JSON.stringify(j.golden.mismatches)}</span>`)
    : '';
  const pos = Object.entries(j.positions||{}).map(
    ([pid,tl])=>`<div><code>${pid}</code> : ${tl.join(' → ')}</div>`).join('')
    || '<span class="muted">ポジションなし</span>';
  const rows = (j.recent_events||[]).slice(-10).reverse().map(e=>
    `<tr><td>${e.seq}</td><td>${e.kind}</td><td>${e.bar_time||''}</td>
     <td><code>${JSON.stringify(e.data).slice(0,140)}</code></td></tr>`).join('');
  return `<div style="margin-bottom:14px">
    <div><strong><code>${j.name}</code></strong> ${golden}</div>
    <div>${pos}</div>
    <table><thead><tr><th>seq</th><th>kind</th><th>bar_time</th><th>data</th></tr></thead>
    <tbody>${rows}</tbody></table>
  </div>`;
}
async function refreshAllJournals(){
  try {
    const j = await (await fetch('/api/journals')).json();
    const el = document.getElementById('allJournals');
    el.innerHTML = (j.files||[]).length
      ? j.files.map(renderJournalBlock).join('<hr style="border-color:#30363d">')
      : '<span class="muted">work/journal/ にファイルがありません</span>';
  } catch(e){}
}
setInterval(refreshAccounts, 10000);
setInterval(refreshAllJournals, 10000);
refreshAccounts(); refreshAllJournals();
</script>
</body>
</html>
"""

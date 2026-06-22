"""`python -m infers.dashboard` 入口。

127.0.0.1 のみで監視ダッシュボードを起動し、操作トークンをコンソールに
1 回だけ表示する。Ctrl+C で停止 (稼働中のライブセッションがあれば安全停止)。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from infers.dashboard.controller import LiveController
from infers.dashboard.server import make_server


def _account_specs(paths: list[str] | None) -> list[tuple[str, str | None]]:
    """--terminal-path のリストから (表示ラベル, パス) を組み立てる。

    ラベルはインストール先フォルダ名から自動導出する (手法名を一切
    ハードコードしない)。未指定時は既定接続 (パス省略=OS既定のアクティブ
    ターミナル) を1件だけ表示する。"""
    if not paths:
        return [("既定", None)]
    return [(Path(p).parent.name or p, p) for p in paths]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="infers.dashboard", description="INFERS ローカル監視ダッシュボード")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--symbol", default="XAUUSD",
                   help="ジャーナル監視の既定銘柄 (起動前の表示用)")
    p.add_argument("--terminal-path", dest="terminal_paths", action="append",
                   metavar="PATH",
                   help="読み取り専用で口座情報を表示するMT5ターミナルの "
                        "terminal64.exe パス。複数手法を別ターミナルで同時稼働"
                        "させている場合は手法ごとに繰り返し指定する。省略時は "
                        "既定接続 (パス省略) を1件のみ表示する")
    args = p.parse_args(argv)

    # Windows コンソール (cp932) での日本語文字化け回避 (journal.py と同方針)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass

    controller = LiveController()
    httpd, token = make_server(port=args.port, default_symbol=args.symbol,
                               controller=controller,
                               account_specs=_account_specs(args.terminal_paths))

    print(f"INFERS dashboard:  http://127.0.0.1:{args.port}/")
    print(f"TOKEN (X-Infers-Token):  {token}")
    print("操作トークンを画面上部に入力してください。Ctrl+C で停止します。")
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down...", file=sys.stderr)
    finally:
        controller.stop()        # 稼働中ライブセッションを安全停止
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())

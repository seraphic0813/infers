"""`python -m infers.dashboard` 入口。

127.0.0.1 のみで監視ダッシュボードを起動し、操作トークンをコンソールに
1 回だけ表示する。Ctrl+C で停止 (稼働中のライブセッションがあれば安全停止)。
"""

from __future__ import annotations

import argparse
import sys

from infers.dashboard.controller import LiveController
from infers.dashboard.server import make_server


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="infers.dashboard", description="INFERS ローカル監視ダッシュボード")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--symbol", default="XAUUSD",
                   help="ジャーナル監視の既定銘柄 (起動前の表示用)")
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
                               controller=controller)

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

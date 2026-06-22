"""MT5口座の読み取り専用スナップショット (現在接続情報の表示用)。

ダッシュボードはこの関数経由でのみMT5へ触れる。connect → account_info() →
shutdown() を都度行う読み取り専用の問い合わせであり、発注APIには一切触れない
(トレード根幹から完全に分離。CLAUDE.md ダッシュボード分離原則)。

複数のMT5ターミナルを並行稼働させている場合 (例: narrow_focus / smc_bos を
別ターミナルで同時稼働)、`terminal_path` ごとに直列で接続・切断するため
(同一プロセス内でも同時に2接続を維持する必要はない)、既存のライブ稼働中
プロセス (別プロセスで永続接続を保持) を妨げない。
"""

from __future__ import annotations


def account_snapshot(terminal_path: str | None, *, label: str) -> dict:
    """指定ターミナルへ読み取り専用で接続し、口座情報を1回分だけ取得する。"""
    try:
        import MetaTrader5 as mt5  # Windows専用・遅延 import
    except ImportError:
        return {"label": label, "terminal_path": terminal_path,
                "connected": False, "error": "MetaTrader5 package not available"}

    init_kwargs = {"path": terminal_path} if terminal_path else {}
    try:
        if not mt5.initialize(**init_kwargs):
            return {"label": label, "terminal_path": terminal_path,
                    "connected": False, "error": str(mt5.last_error())}
        info = mt5.account_info()
        if info is None:
            return {"label": label, "terminal_path": terminal_path,
                    "connected": False, "error": f"account_info() failed: {mt5.last_error()}"}
        return {
            "label": label, "terminal_path": terminal_path, "connected": True,
            "login": info.login, "server": info.server, "name": info.name,
            "balance": info.balance, "equity": info.equity,
            "currency": info.currency, "leverage": info.leverage,
        }
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass

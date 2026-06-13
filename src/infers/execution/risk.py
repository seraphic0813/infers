"""独立リスクマネージャー (設計書 §6.6 / CLAUDE.md 第1条)。

戦略・AI Gateway とは独立した「拒否権レイヤー」。
AIがどれだけ強いシグナルを出しても、ここで拒否された注文は
ブローカーに到達しない (NO-TRADE が常にデフォルト)。

- 本モジュールは ai/ にも analysis/ にも依存しない (import すら持たない)
- キルスイッチはラッチ式: 一度作動したら明示的な reset() (運用上は
  再起動・人間の確認に相当) まで全新規注文を拒否し続ける
- 設定の動的変更APIは提供しない (稼働中の誤変更防止: 設計書 §6.6)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskConfig:
    """すべて整数単位 (ティック・ロットステップ)。float禁止 (CLAUDE.md 第6条)。"""

    max_position_volume_steps: int      # 1ポジションの最大ロットステップ
    max_total_volume_steps: int         # 全ポジション合計の上限
    max_spread_ticks: int               # これを超えるスプレッド時は新規拒否 (指標スパイク対策)
    daily_loss_limit_tick_steps: int    # 日次最大損失 (ティック×ステップ, 正の値で指定)

    def __post_init__(self) -> None:
        if min(self.max_position_volume_steps, self.max_total_volume_steps,
               self.max_spread_ticks, self.daily_loss_limit_tick_steps) <= 0:
            raise ValueError("all risk limits must be positive")


@dataclass(frozen=True)
class OrderRequest:
    """リスク審査に掛ける新規注文の要約。"""

    symbol: str
    direction: int
    volume_steps: int
    kind: str                           # "PROBE_LIMIT" | "ADD_MARKET" など (ジャーナル用)


@dataclass(frozen=True)
class RiskVerdict:
    approved: bool
    reason: str                         # 拒否理由 (承認時は "OK")

    def __bool__(self) -> bool:
        return self.approved


class RiskManager:
    """全新規注文の最終審査。承認以外のあらゆる経路は拒否 (deny by default)。"""

    def __init__(self, config: RiskConfig) -> None:
        self._cfg = config
        self._kill_switch = False
        self._kill_reason = ""
        self._daily_realized_tick_steps = 0   # 当日実現損益 (負=損失)

    # -- 参照 -----------------------------------------------------------------

    @property
    def kill_switch_engaged(self) -> bool:
        return self._kill_switch

    @property
    def daily_realized_tick_steps(self) -> int:
        return self._daily_realized_tick_steps

    # -- 審査 (拒否権) ------------------------------------------------------------

    def approve(self, req: OrderRequest, *, current_spread_ticks: int,
                open_total_volume_steps: int) -> RiskVerdict:
        """新規注文の承認/拒否。決済注文 (防御) は審査対象外 —
        防御の実行をリスク層が妨げてはならない。"""
        if self._kill_switch:
            return RiskVerdict(False, f"KILL_SWITCH: {self._kill_reason}")
        if req.volume_steps < 1:
            return RiskVerdict(False, "INVALID_VOLUME")
        if req.volume_steps > self._cfg.max_position_volume_steps:
            return RiskVerdict(
                False,
                f"POSITION_VOLUME_CAP: {req.volume_steps} > {self._cfg.max_position_volume_steps}")
        if open_total_volume_steps + req.volume_steps > self._cfg.max_total_volume_steps:
            return RiskVerdict(
                False,
                f"TOTAL_VOLUME_CAP: {open_total_volume_steps}+{req.volume_steps} > "
                f"{self._cfg.max_total_volume_steps}")
        if current_spread_ticks > self._cfg.max_spread_ticks:
            return RiskVerdict(
                False,
                f"SPREAD_ABNORMAL: {current_spread_ticks} > {self._cfg.max_spread_ticks}")
        return RiskVerdict(True, "OK")

    # -- 日次損益とキルスイッチ -----------------------------------------------------

    def record_realized(self, pnl_tick_steps: int) -> None:
        """実現損益を計上。日次損失上限到達でキルスイッチをラッチ作動。"""
        self._daily_realized_tick_steps += pnl_tick_steps
        if self._daily_realized_tick_steps <= -self._cfg.daily_loss_limit_tick_steps:
            self._engage(
                f"daily loss {self._daily_realized_tick_steps} <= "
                f"-{self._cfg.daily_loss_limit_tick_steps}")

    def engage_kill_switch(self, reason: str) -> None:
        """外部要因 (リコンサイル不一致・接続異常) による手動作動。"""
        self._engage(reason)

    def _engage(self, reason: str) -> None:
        if not self._kill_switch:
            self._kill_switch = True
            self._kill_reason = reason

    def new_day(self) -> None:
        """日次カウンタのリセット。キルスイッチは解除しない (ラッチ維持)。"""
        self._daily_realized_tick_steps = 0

    def reset_kill_switch(self) -> None:
        """人間の明示操作 (運用上は原因確認後の再起動) でのみ解除。"""
        self._kill_switch = False
        self._kill_reason = ""

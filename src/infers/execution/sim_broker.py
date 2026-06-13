"""バックテスト用 疑似ブローカー (設計書 §8 / CLAUDE.md 第10・12条)。

- 冪等性: 同一 client_order_id の再発注は無視され、二重建てが起きない
- スプレッド: 買いの約定は ask (= bid + spread) 基準で再現
- 最小ストップ距離: ブローカー制約を発注前に検証 (設計書 §6.5 #6)
- SL判定は保守側: 同一バーで約定とSL接触が併発した場合もSLを執行する
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from infers.data.models import Candle


class BrokerRejection(RuntimeError):
    """ブローカー制約違反 (最小ストップ距離・不正数量など)。"""


@dataclass
class _PendingLimit:
    client_order_id: str
    position_id: str
    direction: int
    price_int: int
    volume_steps: int
    sl_int: int


@dataclass
class _Position:
    position_id: str
    direction: int
    volume_steps: int
    avg_entry_int: int
    sl_int: int


@dataclass(frozen=True)
class BrokerEvent:
    """process_bar が返す執行イベント (エンジンが FSM へ配送する)。"""

    kind: Literal["FILL", "SL_HIT"]
    client_order_id: str
    position_id: str
    price_int: int
    volume_steps: int


class SimBroker:
    """確定足ベースの約定シミュレータ。

    約定モデル (保守側に倒す):
      - 買い指値: バー安値 + spread (=ask最安値) <= 指値価格 で約定、価格改善なし
      - 成行: 直近確定足終値 ± spread で即時約定
      - 買いSL: バー安値 (bid) <= SL でヒット、約定価格はSL丸め (滑りなし)
    イントラバーの到達順序は解決しない (M1解決はフェーズ7のbacktestエンジン)。
    """

    def __init__(self, *, spread_ticks: int, min_stop_distance_ticks: int) -> None:
        if spread_ticks < 0 or min_stop_distance_ticks < 0:
            raise ValueError("spread/min_stop must be >= 0")
        self.spread = spread_ticks
        self.min_stop = min_stop_distance_ticks
        self._seen_order_ids: set[str] = set()
        self._pending: dict[str, _PendingLimit] = {}
        self._positions: dict[str, _Position] = {}
        self._last_close_int: int | None = None

    # -- 参照 -----------------------------------------------------------------

    def position(self, position_id: str) -> _Position | None:
        return self._positions.get(position_id)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    # -- 注文 API (BrokerPort 実装) ---------------------------------------------

    def _idempotent(self, client_order_id: str) -> bool:
        """既知の注文IDなら True (再発注を無視する: CLAUDE.md 第10条)。"""
        if client_order_id in self._seen_order_ids:
            return True
        self._seen_order_ids.add(client_order_id)
        return False

    def _check_stop_distance(self, ref_price_int: int, sl_int: int) -> None:
        if abs(ref_price_int - sl_int) < self.min_stop:
            raise BrokerRejection(
                f"stop too close: |{ref_price_int} - {sl_int}| < min_stop {self.min_stop}")

    def place_limit(self, *, client_order_id: str, position_id: str, direction: int,
                    price_int: int, volume_steps: int, sl_int: int) -> None:
        if self._idempotent(client_order_id):
            return
        if volume_steps < 1:
            raise BrokerRejection("volume must be >= 1 step")
        if direction * (price_int - sl_int) <= 0:
            raise BrokerRejection("sl must be on the losing side")
        self._check_stop_distance(price_int, sl_int)
        self._pending[client_order_id] = _PendingLimit(
            client_order_id=client_order_id, position_id=position_id,
            direction=direction, price_int=price_int,
            volume_steps=volume_steps, sl_int=sl_int,
        )

    def place_market(self, *, client_order_id: str, position_id: str, direction: int,
                     volume_steps: int, sl_int: int) -> int:
        """成行注文。直近確定足終値 ± spread で即時約定し、約定価格を返す。"""
        if self._last_close_int is None:
            raise BrokerRejection("no market data yet — call process_bar first")
        fill = self._last_close_int + direction * self.spread
        if self._idempotent(client_order_id):
            pos = self._positions[position_id]
            return pos.avg_entry_int
        if volume_steps < 1:
            raise BrokerRejection("volume must be >= 1 step")
        self._check_stop_distance(fill, sl_int)
        self._fill_into_position(position_id, direction, fill, volume_steps, sl_int)
        return fill

    def modify_sl(self, *, position_id: str, new_sl_int: int) -> None:
        pos = self._positions.get(position_id)
        if pos is None:
            raise BrokerRejection(f"no open position: {position_id}")
        pos.sl_int = new_sl_int

    def close_volume(self, *, client_order_id: str, position_id: str,
                     volume_steps: int) -> int:
        """部分/全決済。bid/ask 基準の決済価格を返す。"""
        if self._idempotent(client_order_id):
            pos = self._positions.get(position_id)
            return self._last_close_int if self._last_close_int is not None else 0
        pos = self._positions.get(position_id)
        if pos is None or volume_steps > pos.volume_steps:
            raise BrokerRejection("close volume exceeds position")
        assert self._last_close_int is not None
        fill = self._last_close_int - pos.direction * self.spread
        pos.volume_steps -= volume_steps
        if pos.volume_steps == 0:
            del self._positions[position_id]
        return fill

    def cancel(self, *, client_order_id: str) -> None:
        self._pending.pop(client_order_id, None)

    # -- マーケット駆動 -----------------------------------------------------------

    def process_bar(self, candle: Candle) -> list[BrokerEvent]:
        """確定足を1本処理し、約定・SLヒットのイベント列を返す。"""
        if not candle.is_closed:
            raise ValueError("SimBroker accepts closed candles only (CLAUDE.md rule 2)")
        self._last_close_int = candle.c_int
        events: list[BrokerEvent] = []

        # 1) 指値の約定判定 (買い: ask最安値 = l + spread が指値以下で約定)
        for oid in list(self._pending):
            order = self._pending[oid]
            if order.direction > 0:
                touched = candle.l_int + self.spread <= order.price_int
            else:
                touched = candle.h_int - self.spread >= order.price_int
            if touched:
                del self._pending[oid]
                self._fill_into_position(order.position_id, order.direction,
                                         order.price_int, order.volume_steps,
                                         order.sl_int)
                events.append(BrokerEvent(
                    kind="FILL", client_order_id=oid, position_id=order.position_id,
                    price_int=order.price_int, volume_steps=order.volume_steps,
                ))

        # 2) SL判定 (約定後にも評価 = 同一バー併発は保守側でSL執行)
        for pid in list(self._positions):
            pos = self._positions[pid]
            hit = (candle.l_int <= pos.sl_int) if pos.direction > 0 else (candle.h_int >= pos.sl_int)
            if hit:
                volume = pos.volume_steps
                del self._positions[pid]
                events.append(BrokerEvent(
                    kind="SL_HIT", client_order_id=f"{pid}/slhit",
                    position_id=pid, price_int=pos.sl_int, volume_steps=volume,
                ))
        return events

    # -- 内部 ----------------------------------------------------------------------

    def _fill_into_position(self, position_id: str, direction: int,
                            price_int: int, volume_steps: int, sl_int: int) -> None:
        pos = self._positions.get(position_id)
        if pos is None:
            self._positions[position_id] = _Position(
                position_id=position_id, direction=direction,
                volume_steps=volume_steps, avg_entry_int=price_int, sl_int=sl_int,
            )
            return
        if pos.direction != direction:
            raise BrokerRejection("hedging within one position_id is not allowed")
        total = pos.volume_steps + volume_steps
        # 平均建値は切り捨てではなく最近接整数ティック (HALF_EVEN相当の単純四捨五入)
        pos.avg_entry_int = (pos.avg_entry_int * pos.volume_steps
                             + price_int * volume_steps + total // 2) // total
        pos.volume_steps = total

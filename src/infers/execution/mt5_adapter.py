"""MT5ライブ接続アダプター + リアルタイム駆動ループ (設計書 §9 / フェーズ8)。

- MT5LiveBroker: BrokerPort のライブ実装。float が許される唯一の場所 =
  MT5 API 境界 (価格・ロットの送受信時のみ即時変換)。冪等性は
  client_order_id を注文コメントへ刻むことで担保する。
- LiveRunner: 確定足ストリーム (MarketFeed.iter_closed) で TradingLoop を
  駆動する常駐ループ。バックテストと同一のコードパス (CLAUDE.md 第12条)。

ライブ投入は必ずデモ口座から (設計書 §11 / main.py がガードする)。
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from infers.ai.gateway import AiGateway
from infers.core.execution import ExecutionModel
from infers.core.loop import ProviderOutput, SignalProvider, TradingLoop
from infers.data.feed import FeedError, MarketFeed
from infers.core.models import Candle, SymbolSpec, Timeframe
from infers.execution.risk import RiskManager
from infers.execution.sim_broker import BrokerEvent
from infers.execution.sm import FsmConfig, PositionFSM, PosState

_HOLDING_STATES = (PosState.PROBE, PosState.ADD, PosState.SL_AT_BE, PosState.RUNNER)

# MT5 の注文コメント上限 (超過すると order_send がターミナルへ届く前に失敗する)。
MT5_COMMENT_MAX = 31
_PID_KEY_LEN = 10

# 新規建て (未保有の position_id に対する発注)。失敗しても見送りで済む操作。
_ENTRY_OPS = frozenset({"place_limit", "place_market"})


def pid_key(position_id: str) -> str:
    """position_id → 注文コメント用の短い決定論キー (16進のため '/' を含まない)。"""
    return hashlib.sha1(position_id.encode("utf-8")).hexdigest()[:_PID_KEY_LEN]


def order_comment(client_order_id: str, position_id: str) -> str:
    """client_order_id を MT5 コメント上限 (31文字) 内の決定論文字列へ写す。

    形式は 'key/seq/tag' (key = pid_key(position_id))。pid 部を短縮するだけなので
    poll_events の '/probe_limit' 判定はそのまま効く。
    """
    if client_order_id.startswith(position_id):
        suffix = client_order_id[len(position_id):]
    else:
        suffix = "/" + hashlib.sha1(client_order_id.encode("utf-8")).hexdigest()[:8]
    return (pid_key(position_id) + suffix)[:MT5_COMMENT_MAX]


class OrderRejected(FeedError):
    """order_send が成功コードを返さなかった (発注・変更・取消のいずれか)。

    LiveRunner が捕捉し、新規建てなら ORDER_REJECT として見送り、
    防御側 (決済・SL変更・取消・既存玉への追加) ならキルスイッチを作動する。
    """

    def __init__(self, *, op: str, client_order_id: str | None,
                 position_id: str | None, retcode: int | None,
                 last_error: object, comment: str | None) -> None:
        self.op = op
        self.client_order_id = client_order_id
        self.position_id = position_id
        self.retcode = retcode
        self.last_error = last_error
        self.comment = comment
        super().__init__(f"order_send failed ({op}): retcode={retcode} {last_error}")

    def to_dict(self) -> dict:
        return {"op": self.op, "client_order_id": self.client_order_id,
                "position_id": self.position_id, "retcode": self.retcode,
                "last_error": str(self.last_error), "comment": self.comment}


# ---------------------------------------------------------------------------
# リコンサイル (設計書 §6.5 #2) — スナップショット取得と突合を分離
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BrokerPositionState:
    """ブローカー側の実態ポジション (整数ティック/ステップに正規化済み)。"""

    volume_steps: int
    sl_int: int
    avg_entry_int: int


@dataclass(frozen=True)
class BrokerSnapshot:
    """ある瞬間のブローカー実態。positions/pending のキーは position_id
    (注文コメントに刻んだ冪等キー 'pid/seq/tag' の pid 部をパースして復元)。"""

    positions: dict[str, BrokerPositionState]
    pending: frozenset[str]


@dataclass
class ReconcileReport:
    events: list[BrokerEvent] = field(default_factory=list)      # 未処理約定の補正イベント
    sl_repairs: list[tuple[str, int]] = field(default_factory=list)  # (pid, 正しいSL)
    orphans: list[str] = field(default_factory=list)             # ローカルに無い実態ポジション
    mismatches: list[str] = field(default_factory=list)          # 自動補正不能な不一致

    @property
    def ok(self) -> bool:
        """False = 人間確認が必要 (キルスイッチ対象)。"""
        return not self.orphans and not self.mismatches


def reconcile_snapshot(
    snapshot: BrokerSnapshot,
    open_positions: Mapping[str, tuple[PositionFSM, object]],
) -> ReconcileReport:
    """純粋関数: ローカル状態機械とブローカー実態を突合する。

    補正の方針:
      - 切断中に発生した未処理イベントは BrokerEvent として再生する
        (FSMの正規の遷移経路を通す。状態の直接書き換えはしない)
      - ローカルが知らない実態 (orphan)・数量不一致など自動補正が
        危険なものは mismatches に積み、呼び出し側がキルスイッチを作動する
    """
    report = ReconcileReport()

    for pid in snapshot.positions:
        if pid not in open_positions:
            report.orphans.append(pid)

    for pid, (fsm, _plan) in open_positions.items():
        pos = snapshot.positions.get(pid)

        if fsm.state is PosState.PROBE_PENDING:
            if pos is not None:
                # 切断中に打診指値が約定していた → FILLイベントで追いつく
                report.events.append(BrokerEvent(
                    kind="FILL", client_order_id=f"{pid}/reconcile",
                    position_id=pid, price_int=pos.avg_entry_int,
                    volume_steps=pos.volume_steps))
                if pos.sl_int != (fsm.sl_int or pos.sl_int):
                    report.sl_repairs.append((pid, fsm.sl_int))
            elif pid not in snapshot.pending:
                # 指値もポジションも無い: 約定→決済まで切断中に完結した可能性。
                # 建値が復元できないため自動補正せず人間確認に回す (保守側)
                report.mismatches.append(f"{pid}: pending order vanished at broker")

        elif fsm.state in _HOLDING_STATES:
            if pos is None:
                # 保有していたはずのポジションが消滅 → 切断中のSLヒットとして再生
                assert fsm.sl_int is not None
                report.events.append(BrokerEvent(
                    kind="SL_HIT", client_order_id=f"{pid}/reconcile",
                    position_id=pid, price_int=fsm.sl_int,
                    volume_steps=fsm.volume_steps))
            else:
                if pos.volume_steps != fsm.volume_steps:
                    report.mismatches.append(
                        f"{pid}: broker volume {pos.volume_steps} != "
                        f"local {fsm.volume_steps}")
                if fsm.sl_int is not None and pos.sl_int != fsm.sl_int:
                    # SLの正はジャーナル済みのローカル (単調性が保証されている)
                    report.sl_repairs.append((pid, fsm.sl_int))

    return report


class MT5LiveBroker:
    """MetaTrader5 への発注アダプター (BrokerPort 実装 + poll_events)。

    冪等性 (CLAUDE.md 第10条):
      - ローカルの既知ID集合で再送を抑止
      - 注文コメントに client_order_id を刻む → 再起動後のリコンサイルで
        ブローカー側から復元できる (リコンサイルループはフェーズ9 TODO)
    """

    def __init__(self, spec: SymbolSpec, *, magic: int = 26001,
                 deviation_points: int = 20,
                 filling_mode: int | None = None) -> None:
        self.spec = spec
        self.magic = magic
        self.deviation = deviation_points
        # ブローカー毎のフィリング方式。None = mt5.ORDER_FILLING_FOK (デフォルト)。
        # Vantage 等 IOC 専用の場合は mt5.ORDER_FILLING_IOC を渡す。
        # connect() 後に _autodetect_filling() で自動設定される。
        self._filling_mode: int | None = filling_mode
        self._mt5: Any | None = None
        self._seen: set[str] = set()
        self._tickets: dict[str, int] = {}          # client_order_id → ticket
        self._position_tickets: dict[str, int] = {}  # position_id → ticket
        self._pid_by_key: dict[str, str] = {}       # pid_key → position_id (コメント復元用)
        self._pid_by_order: dict[int, str] = {}     # 指値の order ticket → position_id
        self._last_poll = datetime.now(timezone.utc)

    def connect(self, *, terminal_path: str | None = None) -> None:
        if self._mt5 is not None:
            return
        try:
            import MetaTrader5 as mt5  # Windows専用・遅延 import
        except ImportError as e:
            raise FeedError("MetaTrader5 package not available") from e
        init_kwargs = {"path": terminal_path} if terminal_path else {}
        if not mt5.initialize(**init_kwargs):
            raise FeedError(f"mt5.initialize failed: {mt5.last_error()}")
        self._mt5 = mt5
        self._autodetect_filling()

    def _autodetect_filling(self) -> None:
        """シンボルがサポートするフィリング方式を自動検出して設定する。

        filling_mode ビットマスク: FOK=1 / IOC=2 / BOC=4。
        FOK優先、次いでIOC。どちらも未対応なら RETURN (=0) を使う。
        """
        if self._filling_mode is not None:
            return
        mt5 = self._require()
        info = mt5.symbol_info(self.spec.name)
        if info is None:
            return
        fm = info.filling_mode
        if fm & 1:
            self._filling_mode = mt5.ORDER_FILLING_FOK
        elif fm & 2:
            self._filling_mode = mt5.ORDER_FILLING_IOC
        else:
            self._filling_mode = mt5.ORDER_FILLING_RETURN

    def _require(self) -> Any:
        if self._mt5 is None:
            raise FeedError("not connected — call connect() first")
        return self._mt5

    # -- 境界変換 (float はここでのみ発生し即座に消費される: CLAUDE.md 第6条) ------

    def _price(self, ticks: int) -> float:
        return float(self.spec.from_ticks(ticks))

    def _lots(self, steps: int) -> float:
        return float(steps * self.spec.lot_step)

    def _success_codes(self, action: int) -> frozenset[int]:
        """アクション別の成功 retcode。指値の成功は PLACED(10008) であり DONE ではない。"""
        mt5 = self._require()
        done = mt5.TRADE_RETCODE_DONE
        placed = getattr(mt5, "TRADE_RETCODE_PLACED", 10008)
        partial = getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010)
        no_changes = getattr(mt5, "TRADE_RETCODE_NO_CHANGES", 10025)
        if action == mt5.TRADE_ACTION_PENDING:
            return frozenset({placed, done})
        if action == mt5.TRADE_ACTION_DEAL:
            # 部分約定でも建玉は実在する (数量ズレはリコンサイルが検出する)
            return frozenset({done, partial})
        if action == mt5.TRADE_ACTION_SLTP:
            return frozenset({done, no_changes})   # 同値SLへの変更は NO_CHANGES
        return frozenset({done})

    def _send(self, request: dict, *, op: str, client_order_id: str | None = None,
              position_id: str | None = None) -> Any:
        mt5 = self._require()
        result = mt5.order_send(request)
        retcode = getattr(result, "retcode", None)
        if result is None or retcode not in self._success_codes(request["action"]):
            raise OrderRejected(op=op, client_order_id=client_order_id,
                                position_id=position_id, retcode=retcode,
                                last_error=mt5.last_error(),
                                comment=request.get("comment"))
        return result

    def _comment(self, client_order_id: str, position_id: str) -> str:
        self._pid_by_key[pid_key(position_id)] = position_id
        return order_comment(client_order_id, position_id)

    def _pid_from_comment(self, comment: str) -> str:
        key = (comment or "").split("/", 1)[0]
        return self._pid_by_key.get(key, key)

    # -- BrokerPort 実装 ----------------------------------------------------------

    def place_limit(self, *, client_order_id: str, position_id: str, direction: int,
                    price_int: int, volume_steps: int, sl_int: int) -> None:
        if client_order_id in self._seen:
            return
        mt5 = self._require()
        req: dict = {
            "action": mt5.TRADE_ACTION_PENDING,
            "symbol": self.spec.name,
            "volume": self._lots(volume_steps),
            "type": mt5.ORDER_TYPE_BUY_LIMIT if direction > 0 else mt5.ORDER_TYPE_SELL_LIMIT,
            "price": self._price(price_int),
            "sl": self._price(sl_int),
            "deviation": self.deviation,
            "magic": self.magic,
            # 冪等キーをブローカー側にも残す (31文字上限に収まる短縮形)
            "comment": self._comment(client_order_id, position_id),
            "type_time": mt5.ORDER_TIME_GTC,
        }
        if self._filling_mode is not None:
            req["type_filling"] = self._filling_mode
        result = self._send(req, op="place_limit", client_order_id=client_order_id,
                            position_id=position_id)
        self._seen.add(client_order_id)
        self._tickets[client_order_id] = result.order
        self._pid_by_order[result.order] = position_id

    def place_market(self, *, client_order_id: str, position_id: str, direction: int,
                     volume_steps: int, sl_int: int) -> int:
        if client_order_id in self._seen:
            return 0
        mt5 = self._require()
        req: dict = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.spec.name,
            "volume": self._lots(volume_steps),
            "type": mt5.ORDER_TYPE_BUY if direction > 0 else mt5.ORDER_TYPE_SELL,
            "sl": self._price(sl_int),
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": self._comment(client_order_id, position_id),
        }
        if self._filling_mode is not None:
            req["type_filling"] = self._filling_mode
        result = self._send(req, op="place_market", client_order_id=client_order_id,
                            position_id=position_id)
        self._seen.add(client_order_id)
        self._position_tickets.setdefault(position_id, result.order)
        return self.spec.float_to_ticks(result.price)

    def modify_sl(self, *, position_id: str, new_sl_int: int) -> None:
        mt5 = self._require()
        ticket = self._position_tickets.get(position_id)
        if ticket is None:
            raise FeedError(f"unknown position: {position_id}")
        self._send({
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": self.spec.name,
            "position": ticket,
            "sl": self._price(new_sl_int),
        }, op="modify_sl", position_id=position_id)

    def close_volume(self, *, client_order_id: str, position_id: str,
                     volume_steps: int) -> int:
        if client_order_id in self._seen:
            return 0
        mt5 = self._require()
        ticket = self._position_tickets.get(position_id)
        if ticket is None:
            raise FeedError(f"unknown position: {position_id}")
        pos = next(iter(mt5.positions_get(ticket=ticket) or []), None)
        if pos is None:
            raise FeedError(f"position ticket {ticket} not found at broker")
        opposite = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.spec.name,
            "volume": self._lots(volume_steps),
            "type": opposite,
            "position": ticket,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": self._comment(client_order_id, position_id),
        }
        if self._filling_mode is not None:
            req["type_filling"] = self._filling_mode
        result = self._send(req, op="close_volume", client_order_id=client_order_id,
                            position_id=position_id)
        self._seen.add(client_order_id)
        return self.spec.float_to_ticks(result.price)

    def cancel(self, *, client_order_id: str) -> None:
        ticket = self._tickets.get(client_order_id)
        if ticket is None:
            return
        mt5 = self._require()
        self._send({"action": mt5.TRADE_ACTION_REMOVE, "order": ticket},
                   op="cancel", client_order_id=client_order_id,
                   position_id=self._pid_by_order.get(ticket))

    # -- 約定イベントの取得 --------------------------------------------------------

    def snapshot(self) -> BrokerSnapshot:
        """ブローカー実態 (ポジション・未約定指値) を冪等キー単位で取得する。

        position_id は注文コメント 'pid/seq/tag' の pid 部から復元する。
        reconcile_snapshot() と組み合わせて再起動・再接続時の同期に使う。
        """
        mt5 = self._require()
        positions: dict[str, BrokerPositionState] = {}
        for pos in (mt5.positions_get(symbol=self.spec.name) or []):
            if pos.magic != self.magic:
                continue
            # 既知チケットを優先 (ブローカーがコメントを保持しない場合もある)。
            # 次いでコメントのキーを既知の position_id へ戻す。未知ならキーのまま
            # (= orphan として報告される)。
            pid = next((p for p, t in self._position_tickets.items()
                        if t == pos.ticket), "") or self._pid_from_comment(pos.comment)
            if not pid:
                continue
            self._position_tickets.setdefault(pid, pos.ticket)
            positions[pid] = BrokerPositionState(
                volume_steps=int(round(pos.volume / float(self.spec.lot_step))),
                sl_int=self.spec.float_to_ticks(pos.sl),
                avg_entry_int=self.spec.float_to_ticks(pos.price_open),
            )
        pending: set[str] = set()
        for order in (mt5.orders_get(symbol=self.spec.name) or []):
            if order.magic != self.magic:
                continue
            pid = self._pid_by_order.get(order.ticket) or self._pid_from_comment(
                order.comment)
            if pid:
                pending.add(pid)
                self._tickets.setdefault(order.comment, order.ticket)
        return BrokerSnapshot(positions=positions, pending=frozenset(pending))

    def equity(self) -> "Decimal":
        """口座残高 (equity) をアカウント通貨建てで返す (EquityProvider 実装)。

        MT5 の account_info().equity はアカウント通貨建てのため、
        VolumeSizerConfig.tick_value_per_step も同通貨で設定すること。
        float→Decimal 変換はここのみ (CLAUDE.md 第6条: float は API 境界のみ)。
        """
        from decimal import Decimal
        mt5 = self._require()
        info = mt5.account_info()
        if info is None:
            raise FeedError("account_info() failed")
        return Decimal(str(info.equity))

    def tick_value_per_step(self, symbol: str) -> "Decimal":
        """1tick × 1step の損益をアカウント通貨建てで返す (VolumeSizer 設定用)。

        MT5 の trade_tick_value はアカウント通貨建て / 1.0 ロット。
        lot_step を掛けて 1 step 当たりに変換する。
        """
        from decimal import Decimal
        mt5 = self._require()
        info = mt5.symbol_info(symbol)
        if info is None:
            raise FeedError(f"symbol_info({symbol!r}) failed")
        tick_value_per_lot = Decimal(str(info.trade_tick_value))
        return tick_value_per_lot * self.spec.lot_step

    def poll_events(self) -> list[BrokerEvent]:
        """前回ポーリング以降の約定 (指値FILL / SLヒット) をイベント化する。

        deals のコメント (=client_order_id) と entry 種別から復元する。
        起動時・再接続時の包括同期は snapshot() + reconcile_snapshot() が担う。
        """
        mt5 = self._require()
        now = datetime.now(timezone.utc)
        deals = mt5.history_deals_get(self._last_poll, now) or []
        self._last_poll = now
        events: list[BrokerEvent] = []
        for deal in deals:
            if deal.magic != self.magic:
                continue
            comment = deal.comment or ""
            price_int = self.spec.float_to_ticks(deal.price)
            volume_steps = int(round(deal.volume / float(self.spec.lot_step)))
            filled_limit = self._pid_by_order.get(getattr(deal, "order", None))
            if deal.entry == mt5.DEAL_ENTRY_IN and (filled_limit or "/probe_limit" in comment):
                position_id = filled_limit or self._pid_from_comment(comment)
                self._position_tickets.setdefault(position_id, deal.position_id)
                events.append(BrokerEvent(kind="FILL", client_order_id=comment,
                                          position_id=position_id,
                                          price_int=price_int, volume_steps=volume_steps))
            elif deal.entry == mt5.DEAL_ENTRY_OUT and deal.reason == mt5.DEAL_REASON_SL:
                position_id = next(
                    (pid for pid, t in self._position_tickets.items()
                     if t == deal.position_id), "")
                if position_id:
                    events.append(BrokerEvent(kind="SL_HIT", client_order_id=comment,
                                              position_id=position_id,
                                              price_int=price_int,
                                              volume_steps=volume_steps))
        return events

    def current_spread_ticks(self) -> int:
        mt5 = self._require()
        tick = mt5.symbol_info_tick(self.spec.name)
        if tick is None:
            raise FeedError(f"no tick for {self.spec.name}")
        return self.spec.float_to_ticks(tick.ask) - self.spec.float_to_ticks(tick.bid)


# ---------------------------------------------------------------------------
# リアルタイム駆動ループ
# ---------------------------------------------------------------------------

class LiveRunner:
    """確定足ストリームで TradingLoop を駆動する常駐ループ。

    event_source / spread_fn を差し替えることで、結合テストでは
    SimBroker・合成フィードによる完全オフライン検証ができる
    (本番では MT5LiveBroker.poll_events / current_spread_ticks)。
    """

    def __init__(
        self,
        *,
        feed: MarketFeed,
        spec: SymbolSpec,
        tf: Timeframe,
        broker,                                   # BrokerPort
        provider: SignalProvider,
        gateway: "AiGateway | None",
        risk: RiskManager,
        fsm_config: FsmConfig,
        event_source: Callable[[Candle], list[BrokerEvent]] | None = None,
        spread_fn: Callable[[], int] | None = None,
        journal=None,                             # JournalSink (ライブのみ)
        reconcile_every_bars: int = 12,           # 定期リコンサイル間隔 (0=無効。M5で12=1時間)
        warm_bars: int = 0,                       # 起動前にプロバイダーへ食わせる歴史バー数 (0=無効)
        volume_sizer=None,                        # VolumeSizer | None
        execution_factory: "Callable[..., ExecutionModel] | None" = None,
        heartbeat_interval_s: float = 300.0,       # 無音検知用ハートビート間隔(改善プランT13。0で無効)
        journal_eval: bool = False,               # 毎バーの評価結果をEVALとして記録(可視化。既定OFF)
    ) -> None:
        self._feed = feed
        self._spec = spec
        self._tf = tf
        self._broker = broker
        self._provider = provider
        self._risk = risk
        self._event_source = event_source or (lambda _c: broker.poll_events())
        self._spread_fn = spread_fn or broker.current_spread_ticks
        self._heartbeat_interval_s = heartbeat_interval_s
        self._journal = journal
        self._journal_eval = journal_eval
        # ライブ EquityProvider: MT5LiveBroker が equity() を実装しているので直接注入。
        equity_prov = broker if volume_sizer is not None else None
        self.loop = TradingLoop(broker=broker, gateway=gateway,
                                risk=risk, fsm_config=fsm_config, journal=journal,
                                expiry_sink=getattr(provider, "notify_probe_expired",
                                                    None),
                                volume_sizer=volume_sizer,
                                equity_provider=equity_prov,
                                execution_factory=execution_factory)
        # リコンサイルの継続実行 (フェーズ8 #6): 起動時に加え、稼働中も
        # (a) 定期 (reconcile_every_bars 本ごと) と (b) フィード再接続復帰直後に
        # 状態を再同期する。再接続検知はフィードの reconnect_count 増分で行う。
        self._reconcile_every_bars = reconcile_every_bars
        self._bars_since_reconcile = 0
        self._last_reconnect_seen = getattr(feed, "reconnect_count", 0)
        self._warm_bars = warm_bars
        # キルスイッチ作動の一過性検知 (改善プランT12): False→True遷移した瞬間のみ
        # 1回だけジャーナルへ記録する (毎バー同じイベントを重複記録しないため)。
        self._kill_switch_journaled = False
        # フィード停止検知: 最後に処理した確定足の close_time と、停止を記録済みか。
        # ハートビートスレッドは MT5 API を呼ばず、これらとフィードの
        # last_activity_utc (フィード側スレッドが更新) だけを読む。
        self._last_bar_close: datetime | None = None
        self._started_at: datetime | None = None
        self._feed_stale = False

    def reconcile(self, *, reason: str = "startup") -> ReconcileReport:
        """ローカル状態機械とブローカー実態の強制同期 (起動時・再接続時・定期)。

        - 未処理イベント (切断中の約定/SLヒット) を FSM の正規経路で再生
        - SLのズレはローカル (単調性保証済みのジャーナル値) を正として修復
        - 自動補正不能 (orphan/数量不一致) はキルスイッチを作動して新規停止
        """
        snapshot = self._broker.snapshot()
        report = reconcile_snapshot(snapshot, self.loop.open_positions)
        self.loop.on_broker_events(report.events)
        for pid, sl_int in report.sl_repairs:
            self._broker.modify_sl(position_id=pid, new_sl_int=sl_int)
        if not report.ok:
            self._risk.engage_kill_switch(
                "RECONCILE_MISMATCH: " + "; ".join(report.orphans + report.mismatches))
        if self._journal is not None:
            # リコンサイル結果も「判断」として残す (CLAUDE.md 第11条)。
            self._journal.record("RECONCILE", {
                "reason": reason,
                "ok": report.ok,
                "replayed_events": len(report.events),
                "sl_repairs": len(report.sl_repairs),
                "orphans": list(report.orphans),
                "mismatches": list(report.mismatches),
            })
        self._bars_since_reconcile = 0
        return report

    def _maybe_reconcile(self, reason: str) -> None:
        """ブローカーが snapshot を提供する場合のみ同期する (Sim等は no-op)。"""
        if hasattr(self._broker, "snapshot"):
            self.reconcile(reason=reason)

    def _maybe_journal_kill_switch(self) -> None:
        """キルスイッチが False→True に遷移した瞬間だけ1回ジャーナルへ記録する
        (改善プランT12: ダッシュボードの口座横断モニタが読み取る想定の信号)。

        トリガー種別(リコンサイル不一致・日次損失上限など)を問わず、
        `self._risk.kill_switch_engaged` の状態を見るだけの一般化した検知にする
        (将来トリガーが増えても本メソッドの変更は不要)。
        """
        if not self._risk.kill_switch_engaged or self._kill_switch_journaled:
            return
        self._kill_switch_journaled = True
        if self._journal is not None:
            self._journal.record("KILL_SWITCH_ENGAGED", {"reason": self._risk.kill_reason})

    def _warm_provider(self) -> "datetime | None":
        """プロバイダーをウォームアップする (ブローカー/ループは通さない)。

        macro_tf=D1 の ATR14 準備には 14 D1 本が必要だが、起動直後は
        iter_closed が未来の新規バーしか流さないため、歴史データが溜まる
        まで 14日以上エントリーが出ない。これを防ぐため、run() 開始前に
        get_history で warm_bars 本の歴史 M5 足をプロバイダーのみに食わせる。
        注文・FSM・リコンサイルには一切触れず、指標のみウォームアップする。

        戻り値: ウォームアップした最後の candle.open_time (なければ None)。
        run() がこの時刻以前のバーを iter_closed から受け取った際にスキップするために使う。
        """
        if self._warm_bars <= 0 or not hasattr(self._feed, "get_history"):
            return None
        import sys
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        # warm_bars 本 × TF 幅 だけ過去に遡る (余裕を持って1.5倍の時間幅)
        dur = self._tf.duration
        lookback = timedelta(seconds=dur.total_seconds() * self._warm_bars * 1.5)
        start = now - lookback
        try:
            candles = self._feed.get_history(self._spec, self._tf, start, now)
        except Exception as e:
            print(f"[warn] warm-up fetch failed ({e}); starting without warm-up",
                  file=sys.stderr)
            return None
        last_time: "datetime | None" = None
        for candle in candles:
            self._provider.on_candle(candle)
            last_time = candle.open_time
        # ウォームアップは発注しない素通しだが、自己ミラー式の単一ポジション制約
        # (例: smc_bos) を持つプロバイダは on_candle 呼び出しだけで「建玉中」に
        # なってしまう。発注を一切伴わないウォームアップ後は必ずフラットに戻す
        # (手法固有の語彙を持たない汎用フック呼び出し。CLAUDE.md L0原則)。
        reset_mirror = getattr(self._provider, "reset_position_mirror", None)
        if reset_mirror is not None:
            reset_mirror()
        print(f"[warm-up] fed {len(candles)} historical bars to provider",
              file=sys.stderr)
        return last_time

    def run(self, *, stop: threading.Event | None = None,
            max_bars: int | None = None) -> int:
        """確定足を1本ずつ処理する。処理したバー数を返す。

        確定足主義 (CLAUDE.md 第2条): iter_closed は形成中バーを
        決して流さないため、本ループに未確定データは到達しない。
        リコンサイル (フェーズ8 #6): 起動時に加え、再接続復帰直後 (新規判断の前) と
        一定本数ごとに状態を再同期し「リコンサイル不一致ゼロ」を維持する。
        """
        self._started_at = datetime.now(timezone.utc)
        warm_until = self._warm_provider()
        self._maybe_reconcile("startup")
        self._maybe_journal_kill_switch()
        bars = 0
        heartbeat_stop, heartbeat_thread = self._start_heartbeat()
        try:
            for candle in self._feed.iter_closed(self._spec, self._tf, stop=stop):
                # ウォームアップ済み区間のバーが iter_closed から重複流入した場合はスキップ。
                # (テスト用 FakeFeed や遅延ポーリング時に get_history と iter_closed が重複)
                if warm_until is not None and candle.open_time <= warm_until:
                    continue
                # 切断から復帰した直後は、新規判断の前に状態を実態へ合わせ直す
                seen = getattr(self._feed, "reconnect_count", 0)
                if seen != self._last_reconnect_seen:
                    self._last_reconnect_seen = seen
                    self._guarded(lambda: self._maybe_reconcile("post-reconnect"))

                self._guarded(lambda: self.loop.on_broker_events(self._event_source(candle)))
                output = self._provider.on_candle(candle)
                self._guarded(lambda: self.loop.on_candle(
                    candle, output, spread_ticks=self._spread_fn()))
                self._last_bar_close = candle.close_time
                if self._feed_stale and self._journal is not None:
                    self._feed_stale = False
                    self._journal.record("FEED_RESUMED", {
                        "bar_close": candle.close_time.isoformat()})
                # 可視化(改善: 沈黙の中身): 毎バーの評価結果=どのゲートで見送りに
                # なったか(プランを出したバーは "signal")を EVAL として記録する。
                # ゲートなし手法(--ai-client none)は VERDICT を残さないため、
                # 本フラグがエントリー未発生時の唯一の per-bar 可観測性になる。
                # 取引ロジックには一切関与しない純粋な追記(loop.on_candle 後に置くのは
                # ジャーナルの bar_time が当足へ設定済みになるため)。
                if self._journal_eval and self._journal is not None:
                    reason = getattr(output, "reject_reason", None)
                    n_plans = len(getattr(output, "plans", []) or [])
                    self._journal.record("EVAL", {
                        "reason": reason if reason is not None
                        else ("signal" if n_plans else "unknown"),
                        "n_plans": n_plans,
                    })
                bars += 1

                # 定期リコンサイル (安全網: あらゆる原因のズレを上限間隔内に検出)
                self._bars_since_reconcile += 1
                if (self._reconcile_every_bars
                        and self._bars_since_reconcile >= self._reconcile_every_bars):
                    self._guarded(lambda: self._maybe_reconcile("periodic"))
                self._maybe_journal_kill_switch()

                if max_bars is not None and bars >= max_bars:
                    break
            return bars
        finally:
            heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=1.0)

    def _start_heartbeat(self) -> tuple[threading.Event, "threading.Thread | None"]:
        """無音検知用ハートビート(改善プランT13)。

        確定足が長時間流れない(フィード無音)場合でも、稼働プロセスが生きて
        いることを一定間隔でジャーナルへ記録する。バーの到着有無に依存しない
        独立スレッドで駆動する(バー駆動の記録だけでは「バーが来ない」という
        障害そのものを検知できないため)。
        """
        stop_event = threading.Event()
        if self._journal is None or self._heartbeat_interval_s <= 0:
            return stop_event, None

        def _loop() -> None:
            while not stop_event.wait(self._heartbeat_interval_s):
                self._heartbeat_tick(datetime.now(timezone.utc))

        thread = threading.Thread(target=_loop, daemon=True, name="infers-heartbeat")
        thread.start()
        return stop_event, thread

    def _heartbeat_tick(self, now: datetime) -> None:
        """HEARTBEAT に確定足の遅延を載せ、フィード停止を一度だけ FEED_STALE として記録する。

        停止の判定: 市場は動いている (フィードが直近 3×TF 以内に価格変化を観測) のに、
        最後の確定足 (未処理なら起動時刻) から 3×TF を超えて新しい確定足が来ない。
        週末など市場が止まっている間は価格変化が無いので誤報しない。
        記録のみで自動アクションはしない (T12 の口座横断モニタと同じ方針)。
        """
        threshold = self._tf.duration * 3
        ref = self._last_bar_close or self._started_at
        lag_s = int((now - ref).total_seconds()) if ref is not None else None
        activity = getattr(self._feed, "last_activity_utc", None)
        self._journal.record("HEARTBEAT", {
            "last_bar_close": (self._last_bar_close.isoformat()
                               if self._last_bar_close else None),
            "lag_s": lag_s,
            "feed_active_at": activity.isoformat() if activity else None,
            "offset_pending": bool(getattr(self._feed, "offset_pending", False)),
        })
        market_active = activity is not None and now - activity <= threshold
        if (ref is not None and now - ref > threshold and market_active
                and not self._feed_stale):
            self._feed_stale = True
            self._journal.record("FEED_STALE", {
                "last_bar_close": (self._last_bar_close.isoformat()
                                   if self._last_bar_close else None),
                "lag_s": lag_s,
                "feed_active_at": activity.isoformat(),
            })

    def _guarded(self, step: Callable[[], object]) -> None:
        """発注失敗 (OrderRejected) でプロセスを落とさない (ライブ層のみの安全網)。

        - 新規建て (未保有 position_id への place_*) の失敗: ORDER_REJECT を記録して見送る。
          TradingLoop は place 成功後にのみ建玉登録するため、状態は汚れない。
        - それ以外 (決済・SL変更・取消・既存玉への追加): ローカルとブローカーの整合が
          保証できないため ORDER_ERROR を記録し、キルスイッチ (新規停止) +リコンサイル。
        バックテストの SimBroker は OrderRejected を出さないので、depth50 経路は不変。
        """
        try:
            step()
        except OrderRejected as e:
            data = e.to_dict()
            entry = e.op in _ENTRY_OPS and e.position_id not in self.loop.open_positions
            if entry:
                if self._journal is not None:
                    self._journal.record("ORDER_REJECT", data)
                # 自己ミラー式の単一ポジション制約を持つ手法は、発注していない建玉を
                # 保有中と見なし続けるため、建玉ゼロならフラットへ戻す (汎用フック)。
                reset_mirror = getattr(self._provider, "reset_position_mirror", None)
                if reset_mirror is not None and not self.loop.open_positions:
                    reset_mirror()
                return
            if self._journal is not None:
                self._journal.record("ORDER_ERROR", data)
            self._risk.engage_kill_switch(f"ORDER_ERROR: {e}")
            try:
                self._maybe_reconcile("post-order-error")
            except Exception as e2:             # noqa: BLE001 — 記録して稼働を続ける
                if self._journal is not None:
                    self._journal.record("RECONCILE_ERROR", {
                        "error": f"{type(e2).__name__}: {e2}"})

    def shutdown(self, reason: str = "SHUTDOWN") -> list[str]:
        """未約定指値の取消と残玉の手仕舞い (常駐停止時)。"""
        return self.loop.close_all_open(reason)

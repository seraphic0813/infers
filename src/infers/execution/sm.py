"""ポジション執行状態機械 (設計書 §6.1〜6.4 / CLAUDE.md 第1〜5・9〜10条)。

状態は Enum 一本の有限状態機械で管理する (boolフラグの組合せ禁止)。

  IDLE → PROBE_PENDING → PROBE → ADD → SL_AT_BE → RUNNER → CLOSED
            │(失効/無効化)    ↑         ↑                   ▲
            └─────────────── CLOSED ◀──(SLヒット) ──────────┘

  追撃 (ADD) の2経路:
    (A) PROBE → ADD → SL_AT_BE   (HL確定前にW1ブレイク)
    (B) PROBE → SL_AT_BE → ADD → SL_AT_BE  (HL確定後にW1ブレイク: 手法の心理的優位性)
  どちらの経路も ADD は高々1回 (_add_fired フラグで保証)。

絶対防衛の設計 (CLAUDE.md 第1・3・4条):
  - 本モジュールは LLM に一切依存しない (import すら持たない)
  - SLは利益方向にのみ動く。逆行させる唯一の API (move_sl) は
    SlMonotonicityError を送出して拒否する
  - 建値SL移動のトリガーは dow.py の StructureEvent (安値切り上げ/
    高値切り下げ「確定」) のみ。含み益・pips を受け取る API は存在しない
    ため、マニュアルが禁じる「早計な建値移動」は構造的に不可能
  - すべての判定は確定足 (is_closed=True) でのみ行う
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from enum import Enum, auto
from typing import Callable, Protocol

from infers.analysis.dow import StructureEvent, StructureEventType, TrendState
from infers.analysis.support_resistance import SRZone
from infers.data.models import Candle


class PosState(Enum):
    IDLE = auto()            # 未エントリー
    PROBE_PENDING = auto()   # 打診指値を発注済み (未約定)
    PROBE = auto()           # 打診玉のみ保有
    ADD = auto()             # 追撃玉投入済み
    SL_AT_BE = auto()        # 建値SL移動済み
    RUNNER = auto()          # 半分利確済み、残玉でフィボ目標 or ダウ転換まで伸ばす
    CLOSED = auto()          # 終了 (約定前キャンセル含む)


class TransitionError(RuntimeError):
    """現在状態で許可されない操作。"""


class SlMonotonicityError(RuntimeError):
    """SLを利益方向以外へ動かそうとした (CLAUDE.md 第3条違反の試行)。"""


class BrokerPort(Protocol):
    """執行先の抽象 (Sim / MT5 を差し替え可能に。CLAUDE.md 第12条)。"""

    def place_limit(self, *, client_order_id: str, position_id: str, direction: int,
                    price_int: int, volume_steps: int, sl_int: int) -> None: ...
    def place_market(self, *, client_order_id: str, position_id: str, direction: int,
                     volume_steps: int, sl_int: int) -> int: ...
    def modify_sl(self, *, position_id: str, new_sl_int: int) -> None: ...
    def close_volume(self, *, client_order_id: str, position_id: str,
                     volume_steps: int) -> int: ...
    def cancel(self, *, client_order_id: str) -> None: ...


@dataclass(frozen=True)
class FsmConfig:
    """執行パラメータ (config/thresholds.yaml から注入)。すべて整数ティック。"""

    min_be_distance_ticks: int      # 建値移動を許可する最小スイング距離 (設計書 §6.3 条件2)
    be_offset_ticks: int            # 建値 + 微益 (スプレッド分など)
    breakout_buffer_ticks: int      # 第1波高値超えのバッファ (設計書 §6.2)
    # 半分利確トリガーのRSI利確圏 (設計書 §6.4: RSIが30/70到達)。RSIは0-100。
    rsi_overbought: Decimal = Decimal(70)   # 買いポジの半分利確ライン
    rsi_oversold: Decimal = Decimal(30)     # 売りポジの半分利確ライン
    # ランナーをダウ転換で決済するか。原典のランナー出口は「フィボ目標まで伸ばし、
    # 下方は建値SLで保護」であり、転換クローズは手法に無い (entry-methodology.md ③-2)。
    runner_reversal_exit: bool = False      # True で旧挙動 (M5ダウ転換でランナー即クローズ)


class PositionFSM:
    """1トレード試行 (打診〜終了) を管理する状態機械。

    すべての状態遷移はジャーナル (journal) に記録される (CLAUDE.md 第11条)。
    """

    def __init__(self, *, position_id: str, direction: int,
                 broker: BrokerPort, config: FsmConfig,
                 journal_sink: Callable[[str, dict], None] | None = None) -> None:
        if direction not in (+1, -1):
            raise ValueError("direction must be +1 or -1")
        self.position_id = position_id
        self.direction = direction
        self._broker = broker
        self._cfg = config
        # 追記専用ジャーナルへの遷移シンク (CLAUDE.md 第11条)。None で純粋な
        # インメモリ記録のみ (バックテスト/テスト)。ライブでは JournalWriter.fsm_sink。
        self._journal_sink = journal_sink

        self._state = PosState.IDLE
        self._seq = 0
        self._sl_int: int | None = None
        self._entry_price_int: int | None = None       # 平均建値 (打診→追撃で数量加重更新)
        self._volume_steps = 0                          # 現在保有量 (ロットステップ)
        self._expiry: datetime | None = None
        self._invalidation_price: int | None = None
        self._limit_order_id: str | None = None
        self._add_fired: bool = False                  # 追撃は1ポジションにつき高々1回
        self.journal: list[tuple[str, dict]] = []

    # -- 参照 ------------------------------------------------------------------

    @property
    def state(self) -> PosState:
        return self._state

    @property
    def sl_int(self) -> int | None:
        return self._sl_int

    @property
    def entry_price_int(self) -> int | None:
        """現在保有玉の平均建値 (打診のみなら打診建値、追撃後は数量加重平均)。"""
        return self._entry_price_int

    @property
    def volume_steps(self) -> int:
        return self._volume_steps

    # -- 内部ヘルパー ------------------------------------------------------------

    def _oid(self, tag: str) -> str:
        """決定論的 client_order_id (冪等性: CLAUDE.md 第10条)。"""
        self._seq += 1
        return f"{self.position_id}/{self._seq:03d}/{tag}"

    def _log(self, transition: str, **payload: object) -> None:
        entry = dict(payload, state=self._state.name)
        self.journal.append((transition, entry))
        if self._journal_sink is not None:
            self._journal_sink(transition, entry)

    def _require(self, *allowed: PosState) -> None:
        if self._state not in allowed:
            raise TransitionError(
                f"{self.position_id}: operation not allowed in {self._state.name}")

    def _require_closed_bar(self, candle: Candle) -> None:
        if not candle.is_closed:
            raise ValueError("decisions only on closed bars (CLAUDE.md rule 2)")

    def _profit_side(self, a: int, b: int) -> int:
        """direction 正規化差分: 利益方向に正。"""
        return self.direction * (a - b)

    # -- §6.1 打診 (PROBE) -------------------------------------------------------

    def place_probe(self, *, limit_price_int: int, volume_steps: int, sl_int: int,
                    expiry: datetime, invalidation_price: int) -> None:
        """未来コンフルエンス候補 (失効時刻つき) で打診指値を置く。

        SLは注文と同時に必ず設定される (SLなしの状態は存在しない: 設計書 §6.1)。
        """
        self._require(PosState.IDLE)
        if volume_steps < 2:
            raise ValueError("probe volume must be >= 2 steps (半分利確を可能にするため)")
        if self._profit_side(limit_price_int, sl_int) <= 0:
            raise ValueError("sl must be on the losing side of the limit price")

        self._limit_order_id = self._oid("probe_limit")
        self._broker.place_limit(
            client_order_id=self._limit_order_id, position_id=self.position_id,
            direction=self.direction, price_int=limit_price_int,
            volume_steps=volume_steps, sl_int=sl_int,
        )
        self._sl_int = sl_int
        self._expiry = expiry
        self._invalidation_price = invalidation_price
        self._state = PosState.PROBE_PENDING
        self._log("PLACE_PROBE", limit=limit_price_int, sl=sl_int,
                  volume=volume_steps, expiry=expiry.isoformat())

    def on_bar_pending(self, candle: Candle) -> str | None:
        """未約定の打診指値の失効・無効化チェック (毎確定足)。

        - expiry 経過 → 取消 (合流点は時間依存: 設計書 §5.5)
        - 終値が invalidation_price (エリオット無効化) に抵触 → 取消
        取消理由を返す ("expired" | "invalidated")。取消しなければ None。

        無効化 (invalidated) は「シナリオ自体の崩壊」なので、失効 (expired) の
        「時間切れによる機会損失」とは扱いが異なる (失効リカバリー: 失効のみ
        クールダウン即時解除の対象。entry-methodology.md 失効リカバリー※例外)。
        無効化が優先 (両立時はシナリオ崩壊が支配的でリカバリー対象外)。
        """
        self._require(PosState.PROBE_PENDING)
        self._require_closed_bar(candle)
        assert self._expiry is not None and self._invalidation_price is not None

        expired = candle.close_time >= self._expiry
        invalidated = self._profit_side(candle.c_int, self._invalidation_price) < 0
        if not (expired or invalidated):
            return None

        assert self._limit_order_id is not None
        self._broker.cancel(client_order_id=self._limit_order_id)
        self._state = PosState.CLOSED
        self._log("CANCEL_PROBE", expired=expired, invalidated=invalidated)
        return "invalidated" if invalidated else "expired"

    def on_probe_fill(self, fill_price_int: int, volume_steps: int) -> None:
        self._require(PosState.PROBE_PENDING)
        self._entry_price_int = fill_price_int
        self._volume_steps = volume_steps
        self._state = PosState.PROBE
        self._log("PROBE_FILL", price=fill_price_int, volume=volume_steps)

    # -- §6.2 追撃 (ADD): 第1波高値超えの確定足終値トリガー -------------------------

    def on_wave1_break(self, candle: Candle, w1_extreme_int: int, *,
                       add_volume_steps: int) -> bool:
        """確定足終値 > W1高値 + buffer (買い。売りは対称) で追撃玉を投入する。

        ヒゲのティック抜けでは発火しない (設計書 §6.2)。発火した場合 True。

        呼び出し可能状態: PROBE または SL_AT_BE。
          - PROBE: W1ブレイクが建値SL移動 (HL確定) より先に到達した経路
          - SL_AT_BE: HL確定後にさらに価格が伸びてW1を突破した経路
            (手法の「心理的優位性」: 建値でリスクゼロになった後に追撃できる)

        追撃は1ポジションにつき高々1回 (_add_fired フラグで保証)。
        SL_AT_BE → ADD 遷移後は on_structure_event (ADD → SL_AT_BE) が
        新しい平均建値を基準にSLを再設定する。

        追撃約定後は建値 (`_entry_price_int`) を数量加重平均へ更新する。
        これにより建値SLはポジション全体の損益分岐を基準に置かれ、
        追撃玉だけが大幅マイナスで切られる事故 (P7) を防ぐ。
        """
        self._require(PosState.PROBE, PosState.SL_AT_BE)
        if self._add_fired:
            return False  # 追撃は高々1回 (SL_AT_BE→ADD→SL_AT_BE で再度呼ばれる場合の防護)
        self._require_closed_bar(candle)
        threshold = w1_extreme_int + self.direction * self._cfg.breakout_buffer_ticks
        if self._profit_side(candle.c_int, threshold) <= 0:
            return False

        assert self._sl_int is not None and self._entry_price_int is not None
        add_fill = self._broker.place_market(
            client_order_id=self._oid("add_market"), position_id=self.position_id,
            direction=self.direction, volume_steps=add_volume_steps, sl_int=self._sl_int,
        )
        prev_entry, prev_vol = self._entry_price_int, self._volume_steps
        total_vol = prev_vol + add_volume_steps
        self._entry_price_int = int(
            (Decimal(prev_entry * prev_vol + add_fill * add_volume_steps)
             / Decimal(total_vol)).to_integral_value(rounding=ROUND_HALF_EVEN))
        self._volume_steps = total_vol
        self._add_fired = True
        self._state = PosState.ADD
        self._log("ADD_FILL", close=candle.c_int, threshold=threshold,
                  add_volume=add_volume_steps, add_fill=add_fill,
                  avg_entry=self._entry_price_int)
        return True

    # -- §6.3 建値SL移動: ダウ構造イベント駆動のみ ---------------------------------

    def on_structure_event(self, ev: StructureEvent) -> bool:
        """dow.py の「安値切り上げ確定 (HL)」(買い) / 「高値切り下げ確定 (LH)」(売り)
        のみを建値SL移動のトリガーとして受け付ける。

        含み益・pips を引数に取るAPIは本FSMに存在しない —
        「早計な建値移動」(マニュアル5.2が禁止) は構造的に不可能。
        条件成立で移動した場合 True。

        建値の基準は `_entry_price_int` = ポジション全体の平均建値 (追撃済みなら
        数量加重平均)。打診玉だけの建値ではないため、追撃玉を含めた損益分岐で
        防御される (P7)。
        """
        if self._state not in (PosState.PROBE, PosState.ADD):
            return False  # 建値移動が意味を持つ状態以外では無視 (再移動も不要)

        required = StructureEventType.HL if self.direction > 0 else StructureEventType.LH
        if ev.type is not required:
            return False

        assert self._entry_price_int is not None
        # 条件2 (設計書 §6.3): 確定スイングが建値から十分離れていること
        if self._profit_side(ev.swing.price_int, self._entry_price_int) < self._cfg.min_be_distance_ticks:
            return False

        new_sl = self._entry_price_int + self.direction * self._cfg.be_offset_ticks
        assert self._sl_int is not None
        if self._profit_side(new_sl, self._sl_int) <= 0:
            return False  # 既に建値以上に引き上がっている場合は何もしない

        self.move_sl(new_sl)
        self._state = PosState.SL_AT_BE
        self._log("SL_TO_BREAKEVEN", new_sl=new_sl, trigger_swing=ev.swing.price_int)
        return True

    def move_sl(self, new_sl_int: int) -> None:
        """SL変更の唯一の経路。利益方向への移動のみ許可 (CLAUDE.md 第3条)。"""
        self._require(PosState.PROBE, PosState.ADD, PosState.SL_AT_BE, PosState.RUNNER)
        assert self._sl_int is not None
        if self._profit_side(new_sl_int, self._sl_int) <= 0:
            raise SlMonotonicityError(
                f"SL may only move in profit direction: {self._sl_int} -> {new_sl_int} "
                f"(direction={self.direction})")
        self._broker.modify_sl(position_id=self.position_id, new_sl_int=new_sl_int)
        self._sl_int = new_sl_int
        self._log("MOVE_SL", new_sl=new_sl_int)

    # -- §6.4 半分利確 (厳密に1回): RSI利確圏 or 重要SRゾーン到達 -------------------

    def on_half_tp_signal(self, candle: Candle, rsi_value: Decimal | None,
                          sr_zones: tuple[SRZone, ...] = (),
                          sma90_int: int | None = None) -> bool:
        """半分利確トリガー (entry-methodology.md ③-1)。確定足クローズで以下のいずれか:

          - RSIが利確圏に到達 (買い: rsi >= rsi_overbought / 売り: rsi <= rsi_oversold)
          - 90SMAに接触 (確定足が90SMAを跨ぐ。SMAが建値の利益側にある時のみ)
          - 重要SRゾーンに到達 (買い: 建値より上の RESISTANCE / 売り: 下の SUPPORT)

        状態遷移 SL_AT_BE → RUNNER と不可分のため、RUNNER 以降では状態ガードにより
        何も起きない (厳密一回性)。フィボ161.8%は半分利確ではなく残玉決済の目標
        (on_runner_target) であり、ここでは使わない。
        """
        if self._state is not PosState.SL_AT_BE:
            return False
        self._require_closed_bar(candle)
        assert self._entry_price_int is not None

        rsi_hit = False
        if rsi_value is not None:
            rsi_hit = (rsi_value >= self._cfg.rsi_overbought if self.direction > 0
                       else rsi_value <= self._cfg.rsi_oversold)

        # 90SMA接触 (グランビル利確点): 確定足が90SMAを跨ぎ、かつ SMA が建値の利益側
        # にある (= 利確になる位置) ときのみ。深い押し目買いが90SMAまで戻った瞬間。
        sma_hit = False
        if sma90_int is not None and candle.l_int <= sma90_int <= candle.h_int:
            sma_hit = self._profit_side(sma90_int, self._entry_price_int) > 0

        sr_hit = False
        want_role = "RESISTANCE" if self.direction > 0 else "SUPPORT"
        for z in sr_zones:
            if z.role != want_role:
                continue
            # ゾーンが建値の利益方向にある (=利確になる位置) ものに限る
            near_edge = z.low_int if self.direction > 0 else z.high_int
            if self._profit_side(near_edge, self._entry_price_int) <= 0:
                continue
            # 価格がゾーンに到達 (買い: 高値がゾーン下端以上 / 売り: 安値がゾーン上端以下)
            reached = (candle.h_int >= z.low_int if self.direction > 0
                       else candle.l_int <= z.high_int)
            if reached:
                sr_hit = True
                break

        if not (rsi_hit or sma_hit or sr_hit):
            return False

        half = self._volume_steps // 2
        assert half >= 1
        self._broker.close_volume(
            client_order_id=self._oid("half_tp"), position_id=self.position_id,
            volume_steps=half,
        )
        self._volume_steps -= half
        self._state = PosState.RUNNER
        trigger = "RSI" if rsi_hit else ("SMA90" if sma_hit else "SR")
        self._log("HALF_TAKE_PROFIT", closed=half, runner=self._volume_steps,
                  trigger=trigger,
                  rsi=str(rsi_value) if rsi_value is not None else None)
        return True

    # -- §6.4 残玉決済: フィボ目標到達 or ダウ転換確定 ------------------------------

    def on_runner_target(self, candle: Candle, fib_target_int: int) -> bool:
        """残玉決済① (設計書 §6.4 / 状態図): フィボ目標 (161.8% 等) 到達で全決済。

        確定足の高値 (買い) / 安値 (売り) がフィボ目標に到達したら残玉を手仕舞う。
        """
        if self._state is not PosState.RUNNER:
            return False
        self._require_closed_bar(candle)
        touch_price = candle.h_int if self.direction > 0 else candle.l_int
        if self._profit_side(touch_price, fib_target_int) < 0:
            return False
        self._close_runner("FIB_TARGET")
        return True

    def on_runner_reversal(self, ev: StructureEvent) -> bool:
        """残玉決済② (設計書 §6.4 / 状態図): ダウ転換確定で全決済。

        買いポジは下降転換 (state_after == DOWN)、売りポジは上昇転換
        (state_after == UP) の確定で残玉を手仕舞う。SUSPECT (警戒) では動かない。
        """
        if self._state is not PosState.RUNNER:
            return False
        reversed_to = TrendState.DOWN if self.direction > 0 else TrendState.UP
        if ev.state_after is not reversed_to:
            return False
        self._close_runner("DOW_REVERSAL")
        return True

    def _close_runner(self, reason: str) -> None:
        """残玉の全決済 (CLOSED へ)。close_all と同じ CLOSE_ALL 遷移を記録する。"""
        if self._volume_steps > 0:
            self._broker.close_volume(
                client_order_id=self._oid("runner_close"),
                position_id=self.position_id, volume_steps=self._volume_steps,
            )
        self._volume_steps = 0
        self._state = PosState.CLOSED
        self._log("CLOSE_ALL", reason=reason)

    # -- 終了 ----------------------------------------------------------------------

    def abort_pending(self, reason: str) -> None:
        """未約定の打診指値を取り消して終了する (シャットダウン・データ末尾)。"""
        self._require(PosState.PROBE_PENDING)
        assert self._limit_order_id is not None
        self._broker.cancel(client_order_id=self._limit_order_id)
        self._state = PosState.CLOSED
        self._log("ABORT_PENDING", reason=reason)

    def on_sl_hit(self, fill_price_int: int) -> None:
        """ブローカーからのSL執行通知。どの保有状態からでも CLOSED へ。"""
        self._require(PosState.PROBE, PosState.ADD, PosState.SL_AT_BE, PosState.RUNNER)
        self._volume_steps = 0
        self._state = PosState.CLOSED
        self._log("SL_HIT", price=fill_price_int)

    def close_all(self, reason: str) -> None:
        """残玉の手仕舞い (フィボ最大目標到達・ダウ転換シグナル等)。"""
        self._require(PosState.PROBE, PosState.ADD, PosState.SL_AT_BE, PosState.RUNNER)
        if self._volume_steps > 0:
            self._broker.close_volume(
                client_order_id=self._oid("close_all"), position_id=self.position_id,
                volume_steps=self._volume_steps,
            )
        self._volume_steps = 0
        self._state = PosState.CLOSED
        self._log("CLOSE_ALL", reason=reason)

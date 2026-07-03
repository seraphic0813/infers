"""SimBroker 逆方向スリッページ (約定現実性ストレステスト、改善プラン T7) のテスト。

不変条件:
  - slippage=0 は従来挙動と完全一致 (既定経路の不汚染)。
  - 成行参入・成行決済・保護SL決済は不利方向へ slippage ずれる。
  - 指値約定は指値価格の契約 (不利方向には滑らない) のため対象外。
  - スリッページを増やすとトレードのグロス損益は単調に悪化 (または不変)。
"""

from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from infers.core.models import Candle, Timeframe
from infers.execution.sim_broker import SimBroker

UTC = timezone.utc
T0 = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)


def mk_candle(i: int, h: int, l: int, c: int) -> Candle:
    o = max(l, min(h, c))
    return Candle(symbol="XAUUSD", tf=Timeframe.M5,
                  open_time=T0 + i * Timeframe.M5.duration,
                  o_int=o, h_int=h, l_int=l, c_int=c, volume=1, is_closed=True)


def test_negative_slippage_rejected():
    with pytest.raises(ValueError):
        SimBroker(spread_ticks=2, min_stop_distance_ticks=5, slippage_ticks=-1)


def test_default_slippage_is_zero_noop():
    """slippage 未指定は 0 = 従来の成行約定式と完全一致。"""
    broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
    assert broker.slippage == 0
    broker.process_bar(mk_candle(0, 1005, 995, 1000))
    fill = broker.place_market(client_order_id="e1", position_id="p1",
                               direction=1, volume_steps=2, sl_int=980)
    assert fill == 1000 + 2                     # close + spread、slippage寄与なし


@pytest.mark.parametrize("direction", [1, -1])
def test_market_entry_slips_unfavorably(direction):
    """成行参入は不利方向 (買い=高値側 / 売り=安値側) へ spread+slippage ずれる。"""
    base = SimBroker(spread_ticks=2, min_stop_distance_ticks=5)
    slipped = SimBroker(spread_ticks=2, min_stop_distance_ticks=5, slippage_ticks=3)
    for b in (base, slipped):
        b.process_bar(mk_candle(0, 1005, 995, 1000))
    sl = 900 if direction > 0 else 1100
    f0 = base.place_market(client_order_id="e", position_id="p", direction=direction,
                           volume_steps=2, sl_int=sl)
    f3 = slipped.place_market(client_order_id="e", position_id="p", direction=direction,
                              volume_steps=2, sl_int=sl)
    assert f0 == 1000 + direction * 2
    assert f3 == 1000 + direction * (2 + 3)     # 不利方向へさらに3
    # 買いは高く、売りは安く約定 = いずれも不利
    assert (f3 - f0) * direction == 3


@pytest.mark.parametrize("direction", [1, -1])
def test_market_close_slips_unfavorably(direction):
    """成行決済 (TP) は不利方向へ spread+slippage ずれる。"""
    slipped = SimBroker(spread_ticks=2, min_stop_distance_ticks=5, slippage_ticks=3)
    slipped.process_bar(mk_candle(0, 1005, 995, 1000))
    sl = 900 if direction > 0 else 1100
    slipped.place_market(client_order_id="e", position_id="p", direction=direction,
                         volume_steps=2, sl_int=sl)
    fill = slipped.close_volume(client_order_id="c", position_id="p", volume_steps=2)
    # 決済は建玉方向の逆 = close - direction*(spread+slippage)
    assert fill == 1000 - direction * (2 + 3)


@pytest.mark.parametrize("direction", [1, -1])
def test_sl_hit_slips_beyond_stop(direction):
    """保護SL/トレール決済は SL価格より不利方向へ slippage ずれる。"""
    slipped = SimBroker(spread_ticks=2, min_stop_distance_ticks=5, slippage_ticks=3)
    slipped.process_bar(mk_candle(0, 1005, 995, 1000))
    if direction > 0:
        sl = 980
        slipped.place_market(client_order_id="e", position_id="p", direction=1,
                             volume_steps=2, sl_int=sl)
        events = slipped.process_bar(mk_candle(1, 985, 975, 978))   # 安値がSL割れ
    else:
        sl = 1020
        slipped.place_market(client_order_id="e", position_id="p", direction=-1,
                             volume_steps=2, sl_int=sl)
        events = slipped.process_bar(mk_candle(1, 1025, 1015, 1022))  # 高値がSL超え
    assert [e.kind for e in events] == ["SL_HIT"]
    # 買い: sl-3 (より安く売れる=不利) / 売り: sl+3 (より高く買い戻す=不利)
    assert events[0].price_int == sl - direction * 3


def test_limit_fill_not_slipped():
    """指値約定は指値価格のまま (契約: 不利方向には滑らない)。"""
    slipped = SimBroker(spread_ticks=2, min_stop_distance_ticks=5, slippage_ticks=3)
    slipped.process_bar(mk_candle(0, 1005, 995, 1000))
    slipped.place_limit(client_order_id="e", position_id="p", direction=1,
                        price_int=990, volume_steps=2, sl_int=970)
    events = slipped.process_bar(mk_candle(1, 995, 985, 992))   # l+spread=987<=990 約定
    fills = [e for e in events if e.kind == "FILL"]
    assert len(fills) == 1
    assert fills[0].price_int == 990           # 指値価格そのまま (slippage寄与なし)


@given(slippage=st.integers(min_value=0, max_value=10))
@settings(max_examples=25, deadline=None)
def test_more_slippage_never_improves_pnl(slippage):
    """スリッページを増やすと成行往復のグロス損益は悪化 (または不変)。

    買い: entry=close+(spread+s), exit=close-(spread+s) → 各往復で 2*(spread+s) を失う。
    slippage が大きいほど受取が減る = 単調悪化。
    """
    broker = SimBroker(spread_ticks=2, min_stop_distance_ticks=5,
                       slippage_ticks=slippage)
    broker.process_bar(mk_candle(0, 1005, 995, 1000))
    entry = broker.place_market(client_order_id="e", position_id="p", direction=1,
                                volume_steps=2, sl_int=900)
    broker.process_bar(mk_candle(1, 1015, 1005, 1010))   # 価格上昇
    exit_ = broker.close_volume(client_order_id="c", position_id="p", volume_steps=2)
    gross = exit_ - entry                                 # 買いの往復損益 (per step price差)
    # spread+slippage を出入りで2回払う分だけ、理論上限 (1010-1000=10) から減る
    assert gross == 10 - 2 * (2 + slippage)

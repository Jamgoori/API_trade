import time
from decimal import Decimal as Dd

from hedgebot.models import OrderStatus, Side, Tif
from hedgebot.venues.paper import PaperVenue
from tests.conftest import FakeDataVenue, make_market


def venue(tmp_path, maker_fill="through", **market_kw):
    data = FakeDataVenue("arcus", {"TST-USD": make_market("arcus", "TST-USD", **market_kw)})
    return PaperVenue(data, Dd(10000), state_dir=tmp_path, maker_fill=maker_fill, bbo_cache_s=0), data


def test_post_only_rests_then_fills_when_traded_through(tmp_path):
    v, data = venue(tmp_path)
    o = v.place_order("TST-USD", Side.BUY, Dd(10), Dd("99.99"), Tif.ALO)
    assert o.status is OrderStatus.OPEN
    assert v.order_state(o).status is OrderStatus.OPEN  # still at the touch: not filled under "through"
    data.set_book("TST-USD", "99.95", "99.97")
    st = v.order_state(o)
    assert st.status is OrderStatus.FILLED and st.filled == 10
    acct = v.account()
    assert acct.positions["TST-USD"].size == 10
    assert v.fees_paid == 0  # maker fee 0 on Arcus RWA


def test_touch_mode_fills_at_the_touch(tmp_path):
    v, _ = venue(tmp_path, maker_fill="touch")
    o = v.place_order("TST-USD", Side.SELL, Dd(1), Dd("100.01"), Tif.ALO)
    assert v.order_state(o).status is OrderStatus.FILLED


def test_post_only_that_would_cross_is_rejected(tmp_path):
    v, _ = venue(tmp_path)
    o = v.place_order("TST-USD", Side.BUY, Dd(1), Dd("100.01"), Tif.ALO)
    assert o.status is OrderStatus.REJECTED and o.reject_reason == "POST_ONLY_WOULD_CROSS"


def test_ioc_walks_the_book_and_pays_taker_fee(tmp_path):
    v, data = venue(tmp_path)
    data.set_book("TST-USD", "99.99", "100.01", size="3")
    o = v.place_order("TST-USD", Side.BUY, Dd(5), Dd("100.02"), Tif.IOC)
    assert o.status is OrderStatus.FILLED and o.filled == 5
    assert o.avg_price == (Dd(3) * Dd("100.01") + Dd(2) * Dd("100.02")) / 5
    assert v.fees_paid == o.filled * o.avg_price * Dd("0.0002")
    o2 = v.place_order("TST-USD", Side.BUY, Dd(5), Dd("99"), Tif.IOC)
    assert o2.status is OrderStatus.CANCELED and o2.filled == 0


def test_reduce_only_and_round_trip_pnl(tmp_path):
    v, data = venue(tmp_path)
    assert v.place_order("TST-USD", Side.SELL, Dd(1), Dd("99"), Tif.IOC, reduce_only=True).status is OrderStatus.REJECTED
    v.place_order("TST-USD", Side.BUY, Dd(10), Dd("101"), Tif.IOC)
    data.set_book("TST-USD", "109.99", "110.01")
    v.place_order("TST-USD", Side.SELL, Dd(10), Dd("109"), Tif.IOC, reduce_only=True)
    assert "TST-USD" not in v.positions
    expected = Dd(10) * (Dd("109.99") - Dd("100.01")) - v.fees_paid
    assert v.cash == Dd(10000) + expected


def test_off_hours_initial_margin_blocks_oversized_orders(tmp_path):
    v, _ = venue(tmp_path, off_hours_max_leverage=Dd(2), outside_rth=True)
    v.set_leverage("TST-USD", 10, cross=True)
    # $10k equity at 2x off-hours = $20k max new notional
    assert v.place_order("TST-USD", Side.BUY, Dd(250), Dd("100.02"), Tif.IOC).status is OrderStatus.REJECTED
    assert v.place_order("TST-USD", Side.BUY, Dd(150), Dd("100.02"), Tif.IOC).status is OrderStatus.FILLED


def test_funding_is_charged_hourly(tmp_path):
    v, data = venue(tmp_path, funding_rate_1h=Dd("0.0001"))
    v.place_order("TST-USD", Side.BUY, Dd(10), Dd("100.02"), Tif.IOC)
    cash = v.cash
    v.last_funding_hour -= 2  # two funding hours elapsed
    v.account()
    assert v.cash == cash - Dd(10) * Dd(100) * Dd("0.0001") * 2


def test_isolated_liquidation(tmp_path):
    v, data = venue(tmp_path, isolated_only=True, maintenance_margin_fraction=Dd("0.05"))
    v.set_leverage("TST-USD", 5, cross=True)  # forced isolated
    v.place_order("TST-USD", Side.BUY, Dd(10), Dd("100.02"), Tif.IOC)
    assert v.positions["TST-USD"].isolated
    data.set_book("TST-USD", "80", "80.02")  # -20%: beyond 1/5 - 5% buffer
    acct = v.account()
    assert "TST-USD" not in acct.positions and v.liquidations == 1


def test_state_survives_restart(tmp_path):
    v, data = venue(tmp_path)
    v.place_order("TST-USD", Side.BUY, Dd(3), Dd("100.02"), Tif.IOC)
    v2 = PaperVenue(data, Dd(999), state_dir=tmp_path)
    assert v2.positions["TST-USD"].size == 3 and v2.cash == v.cash


def test_isolated_top_up_moves_liquidation_and_survives_adds(tmp_path):
    v, data = venue(tmp_path, isolated_only=True, maintenance_margin_fraction=Dd("0.05"))
    v.set_leverage("TST-USD", 2, cross=False)
    v.place_order("TST-USD", Side.BUY, Dd(10), Dd("100.02"), Tif.IOC)
    liq_before = v.account().positions["TST-USD"].liquidation_price
    v.add_isolated_margin("TST-USD", Dd(200))
    liq_after = v.account().positions["TST-USD"].liquidation_price
    assert liq_after < liq_before
    v.place_order("TST-USD", Side.BUY, Dd(2), Dd("100.02"), Tif.IOC)  # adding keeps the top-up
    assert v.positions["TST-USD"].isolated_margin > Dd(12) * Dd("100.01") / 2

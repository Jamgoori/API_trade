from decimal import Decimal

from hedgebot.models import OrderStatus, Side, Tif
from hedgebot.venues.hyperliquid import HyperliquidClient, HyperliquidVenue, hl_round_price, hl_tick_size


def test_price_rule_five_sig_figs_and_decimals():
    assert hl_round_price(Decimal("211.0567"), 3, "nearest") == Decimal("211.06")
    assert hl_round_price(Decimal("1239.456"), 2, "down") == Decimal("1239.4")
    assert hl_round_price(Decimal("0.0123456"), 0, "nearest") == Decimal("0.012346")
    assert hl_round_price(Decimal("0.0123456"), 2, "nearest") == Decimal("0.0123")  # capped at 6 - szDecimals
    assert hl_round_price(Decimal("123456.7"), 0, "up") == Decimal("123457")  # integers are always allowed
    assert hl_tick_size(Decimal("211.05"), 3) == Decimal("0.01")


class FakeExchange:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def order(self, *args, **kw):
        self.calls.append((args, kw))
        return self.response


def _venue(response):
    client = HyperliquidClient("mainnet", "0x" + "1" * 40, "0x" + "2" * 64, dexes=["xyz"])
    client._exchange = FakeExchange(response)
    v = HyperliquidVenue(client, "xyz")
    return v, client._exchange


def test_ioc_partial_fill_is_done_with_filled_amount():
    v, ex = _venue({"status": "ok", "response": {"type": "order", "data": {"statuses": [
        {"filled": {"totalSz": "0.4", "avgPx": "211.07", "oid": 77}}]}}})
    st = v.place_order("xyz:NVDA", Side.SELL, Decimal("1"), Decimal("210"), Tif.IOC, reduce_only=True)
    assert st.status is OrderStatus.FILLED and st.filled == Decimal("0.4") and st.avg_price == Decimal("211.07")
    (args, kw), = ex.calls
    assert args[:4] == ("xyz:NVDA", False, 1.0, 210.0) and args[4] == {"limit": {"tif": "Ioc"}} and kw["reduce_only"]


def test_ioc_without_match_is_clean_zero_fill():
    v, _ = _venue({"status": "ok", "response": {"type": "order", "data": {"statuses": [
        {"error": "Order could not immediately match against any resting orders. asset=110001"}]}}})
    st = v.place_order("xyz:NVDA", Side.BUY, Decimal("1"), Decimal("212"), Tif.IOC)
    assert st.status is OrderStatus.CANCELED and st.filled == 0


def test_resting_and_rejected():
    v, _ = _venue({"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 5}}]}}})
    assert v.place_order("xyz:NVDA", Side.BUY, Decimal("1"), Decimal("200"), Tif.ALO).status is OrderStatus.OPEN
    v, _ = _venue({"status": "ok", "response": {"type": "order", "data": {"statuses": [
        {"error": "Insufficient margin to place order."}]}}})
    st = v.place_order("xyz:NVDA", Side.BUY, Decimal("1"), Decimal("200"), Tif.IOC)
    assert st.status is OrderStatus.REJECTED and "margin" in st.reject_reason
    v, _ = _venue({"status": "err", "response": "User or API Wallet does not exist."})
    assert v.place_order("xyz:NVDA", Side.BUY, Decimal("1"), Decimal("200"), Tif.IOC).status is OrderStatus.REJECTED


def test_stop_is_reduce_only_market_trigger():
    v, ex = _venue({"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 9}}]}}})
    from hedgebot.models import MarketInfo
    from tests.conftest import make_market
    m = make_market("hl:xyz", "xyz:NVDA", step_size=Decimal("0.001"))
    m.extra["szDecimals"] = 3
    v._markets = {"xyz:NVDA": m}
    st = v.place_stop("xyz:NVDA", Side.BUY, Decimal("2.5"), Decimal("250.004"))
    assert st.status is OrderStatus.OPEN and st.order_id == "9"
    (args, kw), = ex.calls
    assert args[:4] == ("xyz:NVDA", True, 2.5, 275.0)
    assert args[4] == {"trigger": {"triggerPx": 250.0, "isMarket": True, "tpsl": "sl"}} and kw["reduce_only"]


def test_unified_account_mode_is_refused_before_trading():
    import pytest

    from hedgebot.engine import check_hl_mode
    from hedgebot.venues.hyperliquid import HyperliquidVenue

    class Client:
        can_trade = True
        account_address = "0xabc"
        network = "mainnet"

        def __init__(self, mode):
            self.mode = mode

        def account_mode(self):
            return self.mode

    check_hl_mode({"hl:io": HyperliquidVenue(Client("disabled"), "io")})
    with pytest.raises(SystemExit, match="unifiedAccount"):
        check_hl_mode({"hl:io": HyperliquidVenue(Client("unifiedAccount"), "io")})

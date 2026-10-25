from decimal import Decimal as Dd

from hedgebot.models import Side, Tif
from hedgebot.selftest import check_venue, format_reports, pick_symbols
from hedgebot.venues.paper import PaperVenue
from tests.conftest import FakeDataVenue, make_config, make_market


def paper(tmp_path, name="hl:io", symbol="io:TST", **market_kw):
    data = FakeDataVenue(name, {symbol: make_market(name, symbol, **market_kw)})
    return PaperVenue(data, Dd(1000), state_dir=tmp_path, bbo_cache_s=0)


def test_resting_order_is_placed_and_canceled_without_a_position(tmp_path):
    v = paper(tmp_path)
    rep = check_venue(v, "io:TST", 5, True, fill=False, usd=Dd(12), timeout_s=0, poll_s=0)
    assert rep.ok, format_reports([rep])
    assert [s for s, _, _ in rep.steps] == ["account", "set leverage", "resting order", "cancel"]
    assert not v.orders and not v.positions


def test_fill_round_trip_opens_stops_tops_up_and_closes(tmp_path):
    v = paper(tmp_path, isolated_only=True)
    rep = check_venue(v, "io:TST", 5, True, fill=True, usd=Dd(12), timeout_s=0, poll_s=0)
    assert rep.ok, format_reports([rep])
    steps = [s for s, _, _ in rep.steps]
    assert steps[-5:] == ["open (IOC)", "exchange stop", "cancel stop", "add isolated margin",
                          "close (reduce-only IOC)"]
    assert "isolated" in dict((s, d) for s, _, d in rep.steps)["set leverage"]
    assert not v.positions and not v.stops


def test_symbol_with_an_existing_position_is_left_alone(tmp_path):
    v = paper(tmp_path)
    v.place_order("io:TST", Side.BUY, Dd(1), Dd(101), Tif.IOC)
    rep = check_venue(v, "io:TST", 5, True, fill=True, usd=Dd(12), timeout_s=0, poll_s=0)
    assert not rep.ok and rep.steps[-1][0] == "existing position"
    assert v.positions["io:TST"].size == 1


def test_one_symbol_per_venue_from_the_config():
    cfg = make_config()
    picked = pick_symbols(cfg)
    assert set(picked) == {"arcus", "hl:xyz"}
    assert pick_symbols(cfg, ["hl:xyz"]).keys() == {"hl:xyz"}

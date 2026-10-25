"""Engine wiring on fake market data: start-up, stops, sizing, contained failures, flatten."""

import time
from decimal import Decimal as Dd

import pytest

import hedgebot.engine as engine_mod
from hedgebot.config import Secrets
from hedgebot.engine import Engine, flatten_all
from hedgebot.journal import Journal
from hedgebot.models import Side, Tif
from tests.conftest import D, FakeDataVenue, make_config, make_market


@pytest.fixture
def engine(tmp_path, monkeypatch):
    def build(cfg_overrides=None, capital=None):
        cfg = make_config()
        cfg.capital = capital or {"arcus": D(10000), "hl:xyz": D(5000)}
        cfg.journal_path = str(tmp_path / "j.sqlite")
        cfg.paper.state_dir = str(tmp_path / "paper")
        cfg.rebalance.enabled = False
        for k, v in (cfg_overrides or {}).items():
            setattr(cfg, k, v)
        data = {
            "arcus": FakeDataVenue("arcus", {"TST-USD": make_market("arcus", "TST-USD")}),
            "hl:xyz": FakeDataVenue("hl:xyz", {"xyz:TST": make_market("hl:xyz", "xyz:TST", outside_rth=False)}),
        }
        monkeypatch.setattr(engine_mod, "build_market_venues", lambda c, s, trading: data)
        eng = Engine(cfg, Secrets(), journal=Journal(cfg.journal_path))
        for v in eng.venues.values():
            v.bbo_cache_s = 0
        return eng, data
    return build


def test_first_start_sizes_to_equity_and_persists_it(engine):
    eng, _ = engine(capital={"arcus": D(5000), "hl:xyz": D(2500)})  # paper balances = capital, so x1...
    eng.venues["arcus"].cash += D(7500)  # ...plus a deposit before the first start: x2
    eng.setup()
    assert eng.size_scale() == (Dd("2.00"), 1)
    assert eng.journal.load_pair_state("__sizing__")["scale"] == "2.00"


def test_stops_are_placed_right_after_start_up(engine):
    eng, _ = engine()
    eng.venues["arcus"].place_order("TST-USD", Side.BUY, Dd(100), Dd("100.05"), Tif.IOC)
    eng.venues["hl:xyz"].place_order("xyz:TST", Side.SELL, Dd(100), Dd("99.95"), Tif.IOC)
    eng.run(max_runtime_s=0.01)
    # before the first tick: the hedge short ($10k on $5k equity) gets a stop, the Arcus long
    # ($10k on $10k equity, survives a >50% drop) does not
    assert set(eng.backstops.active) == {("hl:xyz", "xyz:TST")}


def test_a_failing_component_does_not_stop_the_tick(engine):
    eng, data = engine()
    eng.setup()

    def boom():
        raise ZeroDivisionError("bug")

    eng.risk.evaluate = boom
    eng.tick()  # must not raise
    assert eng.executors[0].clip is not None  # the pair still traded


def test_flatten_mirrors_the_hedge_to_what_is_left_of_the_maker(engine):
    eng, data = engine()
    eng.venues["arcus"].place_order("TST-USD", Side.BUY, Dd(30), Dd("100.05"), Tif.IOC)
    eng.venues["hl:xyz"].place_order("xyz:TST", Side.SELL, Dd(30), Dd("99.95"), Tif.IOC)
    eng.setup()
    data["arcus"].set_empty("TST-USD", "bids")  # the maker leg cannot be sold
    left = flatten_all(eng, attempts=2)
    assert eng.account("hl:xyz", 0).position_size("xyz:TST") == -30  # hedge kept: maker still open
    assert left and "T:" in left[0]

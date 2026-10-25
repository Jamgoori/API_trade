import textwrap
import time
from decimal import Decimal as Dd

import pytest

from hedgebot.config import ConfigError, load_config
from hedgebot.models import AccountState, Position
from hedgebot.risk import RiskManager
from tests.conftest import make_config


def write(tmp_path, text):
    p = tmp_path / "c.yaml"
    p.write_text(textwrap.dedent(text))
    return p


def test_example_config_loads():
    cfg = load_config("config.example.yaml")
    assert cfg.venues_needed() == ["arcus", "hl:xyz", "hl:io"]
    assert cfg.capital["hl:io"] == Dd(4500)
    assert {p.group for p in cfg.pairs} == {"rwa", "io"}
    assert cfg.execution.hedge_mode == "passive_first"


def test_unknown_keys_and_bad_values_are_rejected(tmp_path):
    with pytest.raises(ConfigError, match="unknown keys"):
        load_config(write(tmp_path, """
            pairs:
              - {name: A, maker: {venue: arcus, symbol: A-USD}, hedge: {venue: "hl:xyz", symbol: "xyz:A"},
                 notional_usd: 100, levrage: 5}
        """))
    with pytest.raises(ConfigError, match="direction"):
        load_config(write(tmp_path, """
            pairs:
              - {name: A, maker: {venue: arcus, symbol: A-USD}, hedge: {venue: "hl:xyz", symbol: "xyz:A"},
                 notional_usd: 100, direction: sideways}
        """))
    with pytest.raises(ConfigError, match="not listed"):
        load_config(write(tmp_path, """
            hyperliquid: {dexes: [xyz]}
            pairs:
              - {name: A, maker: {venue: arcus, symbol: A-USD}, hedge: {venue: "hl:io", symbol: "io:A"},
                 notional_usd: 100}
        """))


class FakeVenue:
    def __init__(self):
        self.added = []

    def market(self, symbol):
        from tests.conftest import make_market
        return make_market("v", symbol, maintenance_margin_fraction=Dd("0.02"))

    def add_isolated_margin(self, symbol, amount):
        self.added.append((symbol, amount))


class FakeEngine:
    def __init__(self, accounts):
        self.accounts = accounts
        self.venues = {k: FakeVenue() for k in accounts}
        self.halted = False
        self.halt_reason = None
        self.reductions = []
        self.events = []

    def account(self, venue, max_age_s=5):
        return self.accounts[venue]

    def invalidate_account(self, venue):
        pass

    def request_reduction(self, pair, fraction, reason):
        self.reductions.append((pair, fraction))

    def event(self, level, kind, message, **kw):
        self.events.append((level, kind))

    def halt(self, reason, detail):
        self.halted, self.halt_reason = True, reason

    def resume(self, why):
        self.halted = False


def acct(venue, equity, maintenance, positions):
    return AccountState(venue, Dd(equity), Dd(equity), Dd(maintenance), positions, time.time())


def test_low_cross_margin_ratio_derisks_pairs_on_that_venue():
    cfg = make_config()
    pos = {"TST-USD": Position("TST-USD", Dd(10), Dd(100), Dd(100))}
    eng = FakeEngine({"arcus": acct("arcus", 1500, 1000, pos), "hl:xyz": acct("hl:xyz", 5000, 0, {})})
    RiskManager(cfg, eng).evaluate()
    assert eng.reductions == [("T", cfg.risk.derisk_fraction)]


def test_isolated_position_near_liquidation_is_topped_up_from_free_balance():
    cfg = make_config()
    pos = {"xyz:TST": Position("xyz:TST", Dd(-10), Dd(100), Dd(100), liquidation_price=Dd(103), margin_mode="isolated")}
    eng = FakeEngine({"arcus": acct("arcus", 9000, 0, {}), "hl:xyz": acct("hl:xyz", 5000, 0, pos)})
    RiskManager(cfg, eng).evaluate()
    # liq 103 -> target 115 (15% above mark) needs (115 - 103) * 10 * 1.02 = $122.40
    assert eng.venues["hl:xyz"].added == [("xyz:TST", Dd("122.40"))]
    assert not eng.reductions


def test_isolated_position_near_liquidation_is_cut_when_no_free_balance():
    cfg = make_config()
    pos = {"xyz:TST": Position("xyz:TST", Dd(-10), Dd(100), Dd(100), liquidation_price=Dd(103), margin_mode="isolated")}
    eng = FakeEngine({"arcus": acct("arcus", 9000, 0, {}), "hl:xyz": acct("hl:xyz", 0, 0, pos)})
    RiskManager(cfg, eng).evaluate()
    assert eng.venues["hl:xyz"].added == []
    assert eng.reductions and eng.reductions[0][0] == "T"


def test_daily_loss_halts_new_risk():
    cfg = make_config()
    eng = FakeEngine({"arcus": acct("arcus", 10000, 0, {}), "hl:xyz": acct("hl:xyz", 5000, 0, {})})
    rm = RiskManager(cfg, eng)
    rm.evaluate()
    eng.accounts["arcus"] = acct("arcus", 9600, 0, {})  # -2.7% of 15k
    rm.evaluate()
    assert eng.halted and eng.halt_reason == "daily loss"


def test_foreign_positions_are_reported_once():
    cfg = make_config()
    pos = {"BTC-USD": Position("BTC-USD", Dd(1), Dd(1), Dd(1))}
    eng = FakeEngine({"arcus": acct("arcus", 9000, 0, pos), "hl:xyz": acct("hl:xyz", 5000, 0, {})})
    rm = RiskManager(cfg, eng)
    rm.evaluate()
    rm.evaluate()
    assert [k for _, k in eng.events].count("foreign_position") == 1

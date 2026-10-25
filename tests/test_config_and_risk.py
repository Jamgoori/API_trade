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
    assert cfg.capital["hl:io"] == Dd(8000)
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


def held(venue, equity, mark, size=100):
    sym = "TST-USD" if venue == "arcus" else "xyz:TST"
    return acct(venue, equity, 0, {sym: Position(sym, Dd(size), Dd(100), Dd(mark))})


def test_daily_loss_halts_new_risk():
    cfg = make_config()
    eng = FakeEngine({"arcus": held("arcus", 10000, 100), "hl:xyz": acct("hl:xyz", 5000, 0, {})})
    rm = RiskManager(cfg, eng)
    rm.evaluate()
    eng.accounts["arcus"] = held("arcus", 9600, 96)  # the price fell 4% on an (unhedged) position: -2.7% of 15k
    rm.evaluate()
    assert eng.halted and eng.halt_reason == "daily loss"


def test_a_sudden_loss_that_changes_positions_is_not_mistaken_for_a_transfer():
    cfg = make_config()
    eng = FakeEngine({"arcus": held("arcus", 10000, 100), "hl:xyz": acct("hl:xyz", 5000, 0, {})})
    rm = RiskManager(cfg, eng)
    rm.evaluate()
    eng.accounts["arcus"] = acct("arcus", 9500, 0, {})  # liquidated: position gone, $500 lost at once
    rm.evaluate()
    assert eng.halted


def test_foreign_positions_are_reported_once():
    cfg = make_config()
    pos = {"BTC-USD": Position("BTC-USD", Dd(1), Dd(1), Dd(1))}
    eng = FakeEngine({"arcus": acct("arcus", 9000, 0, pos), "hl:xyz": acct("hl:xyz", 5000, 0, {})})
    rm = RiskManager(cfg, eng)
    rm.evaluate()
    rm.evaluate()
    assert [k for _, k in eng.events].count("foreign_position") == 1


def test_money_moving_between_venues_is_not_a_daily_loss():
    cfg = make_config()
    eng = FakeEngine({"arcus": held("arcus", 10000, 100), "hl:xyz": held("hl:xyz", 5000, 100, size=-100)})
    rm = RiskManager(cfg, eng)
    rm.evaluate()
    eng.accounts["arcus"] = held("arcus", 8000, 100)  # $2k sent from Arcus, not arrived yet
    rm.evaluate()
    assert not eng.halted
    eng.accounts["hl:xyz"] = held("hl:xyz", 7000, 100, size=-100)  # it landed
    rm.evaluate()
    assert not eng.halted and rm.day_start_equity == 15000
    eng.accounts["hl:xyz"] = held("hl:xyz", 6700, 103, size=-100)  # then a real loss: the short lost $300
    rm.evaluate()
    assert eng.halted


def test_unreadable_venue_does_not_stop_checks_on_the_others():
    cfg = make_config()
    pos = {"xyz:TST": Position("xyz:TST", Dd(-10), Dd(100), Dd(100), liquidation_price=Dd(103), margin_mode="isolated")}
    eng = FakeEngine({"arcus": acct("arcus", 9000, 0, {}), "hl:xyz": acct("hl:xyz", 5000, 0, pos)})

    def down(venue, max_age_s=5):
        if venue == "arcus":
            from hedgebot.venues.base import VenueError
            raise VenueError("arcus 503", retryable=True)
        return eng.accounts[venue]

    eng.account = down
    RiskManager(cfg, eng).evaluate()
    assert eng.venues["hl:xyz"].added  # the isolated top-up still ran
    assert not eng.halted


def test_a_transfer_made_while_the_bot_was_stopped_is_recognised_after_restart(tmp_path):
    from hedgebot.journal import Journal

    cfg = make_config()
    eng = FakeEngine({"arcus": held("arcus", 10000, 100), "hl:xyz": held("hl:xyz", 5000, 100, size=-100)})
    eng.journal = Journal(str(tmp_path / "j.sqlite"))
    RiskManager(cfg, eng).evaluate()
    eng.accounts["arcus"] = held("arcus", 7000, 100)  # bot stopped; $3k sent to the other venue, in transit
    restarted = RiskManager(cfg, eng)
    restarted.evaluate()
    assert not eng.halted and restarted.day_start_equity == 12000

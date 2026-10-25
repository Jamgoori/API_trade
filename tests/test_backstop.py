import time
from decimal import Decimal as Dd

from hedgebot.backstop import BackstopManager, stop_trigger
from hedgebot.models import AccountState, Position, Side, Tif
from tests.conftest import FakeContext, make_config


def acct(equity, maint, positions):
    return AccountState("v", Dd(equity), Dd(equity), Dd(maint), positions, time.time())


def test_cross_trigger_sits_between_derisk_and_liquidation():
    pos = Position("X", Dd(100), Dd(100), Dd(100))  # $10k long
    a = acct(3000, 500, {"X": pos})
    trig = stop_trigger(a, pos, Dd("1.25"), Dd(2))
    # equity 3000 -> 1.25 * 500 = 625 needs a 23.75% uniform drop
    assert trig == Dd(100) * (1 - Dd("0.2375"))
    derisk_move = (Dd(3000) - Dd("1.7") * 500) / 10000
    liq_move = (Dd(3000) - 500) / Dd(10000)
    assert derisk_move < Dd("0.2375") < liq_move


def test_isolated_trigger_and_short_side():
    pos = Position("X", Dd(-10), Dd(100), Dd(100), liquidation_price=Dd(130), margin_mode="isolated")
    trig = stop_trigger(acct(0, 0, {"X": pos}), pos, Dd("1.25"), Dd(2))
    assert trig == Dd(130) / Dd("1.02") and trig > pos.mark_price
    # already inside the buffer: no stop (the bot is de-risking instead)
    near = Position("X", Dd(-10), Dd(100), Dd(129), liquidation_price=Dd(130), margin_mode="isolated")
    assert stop_trigger(acct(0, 0, {"X": near}), near, Dd("1.25"), Dd(2)) is None


class Engine(FakeContext):
    def __init__(self, cfg, venues, executors):
        super().__init__(cfg, venues)
        self.executors = executors


def test_manager_places_refreshes_and_reports_a_fired_stop(setup_pair):
    cfg, ctx, venues, arcus, hl = setup_pair()
    from hedgebot.executor import PairExecutor
    venues["arcus"].place_order("TST-USD", Side.BUY, Dd(30), Dd("100.02"), Tif.IOC)
    venues["hl:xyz"].place_order("xyz:TST", Side.SELL, Dd(30), Dd("99.98"), Tif.IOC)
    eng = Engine(cfg, venues, [])
    eng.executors = [PairExecutor(cfg.pairs[0], eng)]
    mgr = BackstopManager(eng)
    mgr.refresh()
    # the $3k Arcus long on $10k equity cannot be liquidated (needs a >100% drop): no stop needed there
    assert set(mgr.active) == {("hl:xyz", "xyz:TST")}
    hedge_stop = mgr.active[("hl:xyz", "xyz:TST")]
    assert hedge_stop.order.side is Side.BUY and hedge_stop.trigger > 100
    # hedge market spikes through the short's stop
    hl.set_book("xyz:TST", hedge_stop.trigger + 1, hedge_stop.trigger + Dd("1.02"))
    mgr.refresh()
    assert venues["hl:xyz"].account().position_size("xyz:TST") == 0
    assert "stop_fired" in eng.kinds()

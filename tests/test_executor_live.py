"""Live failure modes found in review: repairs during cool-off, hedge-failure fallbacks, venue errors."""

import random
import time
from decimal import Decimal as Dd

from hedgebot.executor import MAX_UNEXPECTED_ERRORS, PairExecutor, Phase
from hedgebot.models import Side, Tif
from hedgebot.venues.base import VenueError
from tests.test_executor import positions, run_steps


def held(cfg, ctx, venues, qty=30):
    venues["arcus"].place_order("TST-USD", Side.BUY, Dd(qty), Dd("100.02"), Tif.IOC)
    venues["hl:xyz"].place_order("xyz:TST", Side.SELL, Dd(qty), Dd("99.98"), Tif.IOC)
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    ex.rt.direction, ex.rt.phase, ex.rt.target_qty = 1, Phase.HOLD.value, str(qty)
    ex.rt.next_rotation_ts = time.time() + 3600
    return ex


def test_imbalance_is_repaired_during_the_new_clip_cooldown(setup_pair):
    cfg, ctx, venues, arcus, hl = setup_pair()
    ex = held(cfg, ctx, venues)
    venues["hl:xyz"].place_order("xyz:TST", Side.BUY, Dd(10), Dd("100.02"), Tif.IOC, reduce_only=True)
    ex.cooldown_until = time.time() + 600  # e.g. right after a failed hedge
    now = time.time()
    ex.step(now)
    ex.step(now + 1)
    assert positions(venues) == (20, -20)


def test_unwind_clip_whose_hedge_fails_adds_the_maker_leg_back(setup_pair):
    cfg, ctx, venues, arcus, hl = setup_pair(rotation_fraction_min=Dd("0.5"), rotation_fraction_max=Dd("0.5"))
    ex = held(cfg, ctx, venues)
    ex.rt.next_rotation_ts = time.time() - 1
    now = time.time()
    ex.step(now)  # HOLD -> UNWIND, maker SELL clip rests
    assert ex.clip is not None and ex.clip.maker_side is Side.SELL
    hl.set_empty("xyz:TST", "asks")  # the hedge (buy back the short) cannot fill
    px = ex.clip.order.price
    arcus.set_book("TST-USD", px + Dd("0.03"), px + Dd("0.05"))
    ex.step(now + 1)
    m, h = positions(venues)
    assert (m, h) == (30, -30)  # sold maker leg bought back: hedged again, not reduce-only rejected
    assert "hedge_failed" in ctx.kinds()


def test_small_unhedged_remainder_stops_the_ladder(setup_pair):
    cfg, ctx, venues, arcus, hl = setup_pair()
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    ex.step(time.time())
    real, calls = venues["hl:xyz"].place_order, []

    def thin_book(symbol, side, size, price, tif, **kw):  # 9.8 of 10 fill once, then nothing is left
        calls.append(size)
        if len(calls) == 1:
            return real(symbol, side, Dd("9.8"), price, tif, **kw)
        return real(symbol, side, size, Dd(1), tif, **kw)  # far below the bid: IOC fills nothing

    venues["hl:xyz"].place_order = thin_book
    arcus.set_book("TST-USD", "99.90", "99.92")
    ex.step(time.time())
    assert len(calls) == 3 and "hedge_residual" in ctx.kinds() and "hedge_failed" not in ctx.kinds()
    run_steps(ex, 5)
    assert len(calls) == 3  # the $20 rest is not re-laddered every tick


def test_pending_forced_reduction_cancels_a_resting_build_clip(setup_pair):
    cfg, ctx, venues, arcus, hl = setup_pair()
    ex = held(cfg, ctx, venues, qty=10)
    ex.rt.phase, ex.rt.target_qty = Phase.BUILD.value, "30"
    now = time.time()
    ex.step(now)
    assert ex.clip is not None and ex.clip.purpose == "build"
    ctx.forced["T"] = Dd("0.3")
    run_steps(ex, 6, start=now + 1)
    m, h = positions(venues)
    assert m == 7 and h == -7


def test_hedge_venue_errors_fall_back_to_undoing_the_maker_fill(setup_pair):
    cfg, ctx, venues, arcus, hl = setup_pair()
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    ex.step(time.time())

    def down(*a, **k):
        raise VenueError("hl down", retryable=True)

    venues["hl:xyz"].place_order = down
    arcus.set_book("TST-USD", "99.90", "99.92")
    now = time.time()
    ex.step(now)
    assert positions(venues) == (10, 0)  # a blip: no costly undo yet (a lost response may have filled)
    ex.step(now + 10)
    assert positions(venues) == (10, 0) and "hedge_failed" not in ctx.kinds()
    ex.step(now + 25)  # still erroring after 20s: undo the fill on the maker venue
    assert positions(venues) == (0, 0)
    assert "hedge_failed" in ctx.kinds()


def test_unexpected_exception_is_contained_then_disables_the_pair(setup_pair):
    cfg, ctx, venues, arcus, hl = setup_pair()
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))

    def broken(*a, **k):
        raise KeyError("markPx")

    venues["arcus"].bbo = broken
    for _ in range(MAX_UNEXPECTED_ERRORS):
        ex.step(time.time())  # must not raise
    assert ex.disabled_reason and "pair_disabled" in ctx.kinds()

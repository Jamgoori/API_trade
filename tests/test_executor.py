import random
import time
from decimal import Decimal as Dd

from hedgebot.executor import PairExecutor, Phase


def run_steps(ex, n=10, dt=1.0, start=None):
    now = start or time.time()
    for _ in range(n):
        ex.step(now)
        now += dt
    return now


def positions(venues):
    return venues["arcus"].account().position_size("TST-USD"), venues["hl:xyz"].account().position_size("xyz:TST")


def test_maker_fill_is_hedged_immediately(setup_pair):
    cfg, ctx, venues, arcus, hl = setup_pair()
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    ex.step(time.time())
    assert ex.clip is not None and ex.clip.order.tif.value == "ALO"
    arcus.set_book("TST-USD", "99.90", "99.92")  # market trades through our bid
    ex.step(time.time())
    m, h = positions(venues)
    assert m == 10 and h == -10  # $1,000 clip at ~100
    assert any(f["purpose"] == "hedge" and f["liquidity"] == "taker" for f in ctx.fills)
    assert any(f["liquidity"] == "maker" for f in ctx.fills)


def test_builds_to_target_then_holds(setup_pair):
    cfg, ctx, venues, arcus, hl = setup_pair()
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    now = time.time()
    for _ in range(12):
        ex.step(now)
        now += 5
        if ex.clip:  # fill whatever is resting by moving the market through it
            px = ex.clip.order.price
            arcus.set_book("TST-USD", px - Dd("0.05"), px - Dd("0.03"))
            ex.step(now)
            now += 5
        arcus.set_book("TST-USD", "99.99", "100.01")
    m, h = positions(venues)
    assert m == Dd(30) and h == Dd(-30)
    assert ex.rt.phase == Phase.HOLD.value
    assert ex.rt.next_rotation_ts > now


def test_failed_hedge_closes_the_maker_leg(setup_pair):
    cfg, ctx, venues, arcus, hl = setup_pair()
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    ex.step(time.time())
    hl.set_empty("xyz:TST", "bids")  # nothing to sell into on the hedge venue
    arcus.set_book("TST-USD", "99.90", "99.92")
    ex.step(time.time())
    m, h = positions(venues)
    assert m == 0 and h == 0
    assert "hedge_failed" in ctx.kinds()
    assert ex.cooldown_until > time.time()


def test_imbalance_is_repaired_by_shrinking_the_larger_leg(setup_pair):
    cfg, ctx, venues, arcus, hl = setup_pair()
    from hedgebot.models import Side, Tif
    venues["arcus"].place_order("TST-USD", Side.BUY, Dd(10), Dd("100.02"), Tif.IOC)
    venues["hl:xyz"].place_order("xyz:TST", Side.SELL, Dd(7), Dd("99.98"), Tif.IOC)
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    ex.rt.direction, ex.rt.phase, ex.rt.target_qty = 1, Phase.HOLD.value, "10"
    ex.rt.next_rotation_ts = time.time() + 3600
    now = time.time()
    ex.step(now)  # first observation starts the grace timer
    ex.step(now + 1)
    m, h = positions(venues)
    assert m == 7 and h == -7
    assert "imbalance" in ctx.kinds()


def test_rotation_unwinds_and_rebuilds_in_off_hours(setup_pair):
    cfg, ctx, venues, arcus, hl = setup_pair(rotation_fraction_min=Dd("0.5"), rotation_fraction_max=Dd("0.5"))
    from hedgebot.models import Side, Tif
    venues["arcus"].place_order("TST-USD", Side.BUY, Dd(30), Dd("100.02"), Tif.IOC)
    venues["hl:xyz"].place_order("xyz:TST", Side.SELL, Dd(30), Dd("99.98"), Tif.IOC)
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    ex.rt.direction, ex.rt.phase, ex.rt.target_qty = 1, Phase.HOLD.value, "30"
    ex.rt.next_rotation_ts = time.time() - 1  # due now (market is off-hours in the fake)
    now = time.time()
    seen_unwind = False
    for _ in range(40):
        ex.step(now)
        now += 5
        seen_unwind |= ex.rt.phase == Phase.UNWIND.value
        if ex.clip:
            px = ex.clip.order.price
            if ex.clip.maker_side is Side.SELL:
                arcus.set_book("TST-USD", px + Dd("0.03"), px + Dd("0.05"))
            else:
                arcus.set_book("TST-USD", px - Dd("0.05"), px - Dd("0.03"))
            ex.step(now)
            now += 5
        arcus.set_book("TST-USD", "99.99", "100.01")
    assert seen_unwind
    assert ex.rt.rotations_done == 1
    m, h = positions(venues)
    assert m == 30 and h == -30


def test_no_rotation_outside_the_window(setup_pair):
    cfg, ctx, venues, arcus, hl = setup_pair()
    arcus.markets["TST-USD"] = arcus.markets["TST-USD"].__class__(
        **{**arcus.markets["TST-USD"].__dict__, "outside_rth": False})
    from hedgebot.models import Side, Tif
    venues["arcus"].place_order("TST-USD", Side.BUY, Dd(30), Dd("100.02"), Tif.IOC)
    venues["hl:xyz"].place_order("xyz:TST", Side.SELL, Dd(30), Dd("99.98"), Tif.IOC)
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    ex.rt.direction, ex.rt.phase, ex.rt.target_qty = 1, Phase.HOLD.value, "30"
    ex.rt.next_rotation_ts = time.time() - 1
    run_steps(ex, 5)
    assert ex.rt.phase == Phase.HOLD.value and ex.clip is None


def test_forced_reduction_cuts_both_legs_with_iocs(setup_pair):
    cfg, ctx, venues, arcus, hl = setup_pair()
    from hedgebot.models import Side, Tif
    venues["arcus"].place_order("TST-USD", Side.BUY, Dd(30), Dd("100.02"), Tif.IOC)
    venues["hl:xyz"].place_order("xyz:TST", Side.SELL, Dd(30), Dd("99.98"), Tif.IOC)
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    ex.rt.direction, ex.rt.phase, ex.rt.target_qty = 1, Phase.HOLD.value, "30"
    ex.rt.next_rotation_ts = time.time() + 3600
    ctx.forced["T"] = Dd("0.3")
    run_steps(ex, 3)
    m, h = positions(venues)
    assert m == 21 and h == -21
    assert ex.rt.derisk_until > time.time()
    # no rebuild while the de-risk cool-off runs
    run_steps(ex, 5)
    assert positions(venues) == (21, -21)


def test_halt_blocks_new_risk(setup_pair):
    cfg, ctx, venues, arcus, hl = setup_pair()
    ctx.halted = True
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    run_steps(ex, 3)
    assert ex.clip is None and positions(venues) == (0, 0)


def test_basis_guard_waits_for_a_normal_basis(setup_pair):
    cfg, ctx, venues, arcus, hl = setup_pair()
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    ex.basis_ref = Dd(-50)  # normally the maker trades 50bps under the hedge
    ex.basis_ts = time.time()
    ex.step(time.time())  # now at 0bps: buying the maker is 50bps worse than normal
    assert ex.clip is None
    assert any(k == "guard" for k in ctx.kinds())


def test_restart_resumes_from_existing_positions(setup_pair):
    cfg, ctx, venues, arcus, hl = setup_pair()
    from hedgebot.models import Side, Tif
    venues["arcus"].place_order("TST-USD", Side.SELL, Dd(30), Dd("99.98"), Tif.IOC)
    venues["hl:xyz"].place_order("xyz:TST", Side.BUY, Dd(30), Dd("100.02"), Tif.IOC)
    cfg.pairs[0].direction = "auto"
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    ex.step(time.time())
    assert ex.rt.direction == -1  # inferred from the live position, not re-decided
    assert ex.rt.phase == Phase.HOLD.value


def _passive_setup(setup_pair):
    cfg, ctx, venues, arcus, hl = setup_pair()
    cfg.execution.hedge_mode = "passive_first"
    cfg.execution.passive_hedge_timeout_s = 20
    cfg.execution.passive_hedge_max_adverse_bps = Dd(3)
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    return cfg, ctx, venues, arcus, hl, ex


def test_passive_hedge_fills_as_maker(setup_pair):
    cfg, ctx, venues, arcus, hl, ex = _passive_setup(setup_pair)
    now = time.time()
    ex.step(now)
    arcus.set_book("TST-USD", "99.90", "99.92")  # maker leg fills
    ex.step(now + 1)
    assert ex.clip and ex.clip.hedge_order is not None and ex.clip.hedge_order.tif.value == "ALO"
    assert positions(venues) == (10, 0)
    hl.set_book("xyz:TST", "100.02", "100.04")  # buyers lift through our resting hedge offer at 100.01
    ex.step(now + 2)
    ex.step(now + 3)
    assert positions(venues) == (10, -10)
    hedge_fills = [f for f in ctx.fills if f["purpose"] == "hedge"]
    assert hedge_fills and all(f["liquidity"] == "maker" for f in hedge_fills)
    assert ex.clip is None


def test_passive_hedge_escalates_to_taker_after_timeout(setup_pair):
    cfg, ctx, venues, arcus, hl, ex = _passive_setup(setup_pair)
    now = time.time()
    ex.step(now)
    arcus.set_book("TST-USD", "99.90", "99.92")
    ex.step(now + 1)
    assert ex.clip.hedge_order is not None
    for k in range(2, 30, 3):  # nobody trades the hedge book
        ex.step(now + k)
    assert positions(venues) == (10, -10)
    assert any(f["purpose"] == "hedge" and f["liquidity"] == "taker" for f in ctx.fills)


def test_passive_hedge_escalates_on_adverse_move(setup_pair):
    cfg, ctx, venues, arcus, hl, ex = _passive_setup(setup_pair)
    now = time.time()
    ex.step(now)
    arcus.set_book("TST-USD", "99.90", "99.92")
    ex.step(now + 1)
    hl.set_book("xyz:TST", "99.90", "99.92")  # hedge market drops ~10bps before we sold
    for k in range(2, 6):
        ex.step(now + k)
    assert positions(venues) == (10, -10)
    assert any(f["purpose"] == "hedge" and f["liquidity"] == "taker" for f in ctx.fills)


def test_lost_leg_closes_the_other_and_cools_off(setup_pair):
    """An exchange stop / liquidation closed the hedge leg while the bot was away."""
    cfg, ctx, venues, arcus, hl = setup_pair()
    from hedgebot.models import Side, Tif
    venues["arcus"].place_order("TST-USD", Side.BUY, Dd(30), Dd("100.02"), Tif.IOC)
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    ex.rt.direction, ex.rt.phase, ex.rt.target_qty = 1, Phase.HOLD.value, "30"
    ex.rt.next_rotation_ts = time.time() + 3600
    now = time.time()
    run_steps(ex, 4, start=now)
    assert positions(venues) == (0, 0)
    assert "leg_lost" in ctx.kinds()
    assert ex.rt.derisk_until > now + 5 * 3600
    run_steps(ex, 10, start=now + 60)  # no rebuild during the cool-off
    assert positions(venues) == (0, 0) and ex.clip is None

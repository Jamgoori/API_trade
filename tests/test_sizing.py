import random
import time
from decimal import Decimal as Dd

from hedgebot.config import SizingConfig
from hedgebot.executor import PairExecutor, Phase
from hedgebot.sizing import SizeScaler
from tests.test_executor import positions


def scaler(**kw):
    return SizeScaler(SizingConfig(**kw), Dd(1), 0, known=True)


def test_first_start_sizes_to_the_money_that_is_there():
    s = SizeScaler(SizingConfig(), known=False)
    assert s.observe(Dd("1.37"), 0) == ("apply", Dd("1.37"))
    assert s.version == 1


def test_deposit_grows_size_after_it_holds():
    s = scaler(grow_after_min=30)
    assert s.observe(Dd(2), 0) == ("pending", Dd(2))
    assert s.observe(Dd(2), 29 * 60) is None
    assert s.observe(Dd("2.1"), 30 * 60) == ("apply", Dd("2.10"))
    assert (s.scale, s.version) == (Dd("2.10"), 1)


def test_money_in_transit_does_not_cut_positions():
    s = scaler(shrink_after_h=6)
    assert s.observe(Dd("0.85"), 0)[0] == "pending"  # $2.7k of $18k left Arcus for Entropy
    assert s.observe(Dd("0.85"), 3600) is None
    assert s.observe(Dd("1.0"), 2 * 3600) == ("cleared", Dd("1.0"))  # it landed
    assert s.scale == 1 and s.version == 0


def test_lasting_drop_shrinks_and_small_moves_are_ignored():
    s = scaler(shrink_after_h=6, min_change_pct=10)
    assert s.observe(Dd("0.95"), 0) is None
    s.observe(Dd("0.5"), 0)
    assert s.observe(Dd("0.5"), 6 * 3600) == ("apply", Dd("0.50"))


def test_scale_is_clamped():
    s = SizeScaler(SizingConfig(min_scale=Dd("0.25"), max_scale=Dd(4)), known=False)
    assert s.observe(Dd(50), 0) == ("apply", Dd(4))


def build(ex, arcus, now, rounds=12):
    for _ in range(rounds):
        ex.step(now)
        now += 5
        if ex.clip:
            px = ex.clip.order.price
            arcus.set_book("TST-USD", px - Dd("0.05"), px - Dd("0.03"))
            ex.step(now)
            now += 5
        arcus.set_book("TST-USD", "99.99", "100.01")
    return now


def test_executor_grows_the_position_when_the_scale_changes(setup_pair):
    cfg, ctx, venues, arcus, hl = setup_pair()
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    now = build(ex, arcus, time.time())
    assert positions(venues) == (Dd(30), Dd(-30)) and ex.rt.phase == Phase.HOLD.value
    ctx.scale = (Dd(2), 1)
    build(ex, arcus, now, rounds=20)
    assert positions(venues) == (Dd(60), Dd(-60))
    assert ex.rt.scale_seen == 1
    assert "resize" in ctx.kinds()


def test_growth_is_capped_by_the_venue_with_the_least_money():
    s = scaler()
    assert s.target(Dd(2), Dd("1.3")) == Dd("1.3")
    assert s.target(Dd(2), Dd("0.8")) == 1  # a lagging venue blocks growth but does not shrink
    assert s.target(Dd("0.8"), Dd("0.5")) == Dd("0.8")  # shrinking follows the total


def test_a_pending_change_does_not_flap_at_the_threshold():
    s = scaler(min_change_pct=10, grow_after_min=30)
    assert s.observe(Dd("1.101"), 0)[0] == "pending"
    assert s.observe(Dd("1.099"), 600) is None  # still within half the threshold: stays pending
    assert s.observe(Dd("1.101"), 1800) == ("apply", Dd("1.10"))

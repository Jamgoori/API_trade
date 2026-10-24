import random
from decimal import Decimal as Dd

from hedgebot import strategy
from hedgebot.models import Side


def test_carry_sign_convention():
    # hedge longs pay more than maker longs -> long maker / short hedge receives the difference
    assert strategy.carry_apr(1, Dd("0.00001"), Dd("0.00003")) == Dd("0.00002") * 8760
    assert strategy.carry_apr(-1, Dd("0.00001"), Dd("0.00003")) == -Dd("0.00002") * 8760


def test_direction_funding_then_balance():
    big = Dd("0.00002")  # ~17.5% apr difference
    assert strategy.choose_direction("auto", Dd(0), big, Dd("0.03"), Dd(0)) == 1
    assert strategy.choose_direction("auto", big, Dd(0), Dd("0.03"), Dd(0)) == -1
    # no meaningful carry: lean against the existing book on the maker venue
    assert strategy.choose_direction("auto", Dd(0), Dd(0), Dd("0.03"), Dd(5000)) == -1
    assert strategy.choose_direction("auto", Dd(0), Dd(0), Dd("0.03"), Dd(-5000)) == 1
    assert strategy.choose_direction("short_maker", Dd(0), big, Dd("0.03"), Dd(0)) == -1


def test_flip_needs_a_real_edge():
    assert not strategy.should_flip(1, Dd(0), Dd("0.000001"), Dd("0.08"))
    assert strategy.should_flip(1, Dd("0.00001"), Dd(0), Dd("0.08"))  # long maker pays ~17.5% apr


def test_basis_edge_direction():
    # maker cheap vs hedge (basis -10bps) and reference 0: buying the maker leg is +10bps good
    assert strategy.basis_edge_bps(Side.BUY, Dd("99.9"), Dd("100"), Dd(0)).quantize(Dd("0.1")) == Dd("10.0")
    assert strategy.basis_edge_bps(Side.SELL, Dd("99.9"), Dd("100"), Dd(0)).quantize(Dd("0.1")) == Dd("-10.0")


def test_clip_never_leaves_tiny_remainder():
    rng = random.Random(1)
    assert strategy.clip_usd(Dd(800), Dd(500), Dd(1000), rng) == Dd(800)
    assert strategy.clip_usd(Dd(1200), Dd(500), Dd(1000), rng) == Dd(600)
    clip = strategy.clip_usd(Dd(5000), Dd(500), Dd(1000), rng)
    assert Dd(500) <= clip <= Dd(1000)


def test_target_and_margin_helpers():
    assert strategy.target_quantity(-1, Dd(1000), Dd("211.05"), Dd("0.001")) == Dd("-4.738")
    # $1k free at 5x with 10% buffer = $4.5k, but account cap 5x on $2k equity with $8k gross leaves $2k
    assert strategy.addable_notional(Dd(2000), Dd(8000), Dd(1000), Dd(5), Dd(5)) == Dd(2000)
    assert strategy.at_target(Dd("0.95"), Dd(1), Dd("0.08"))
    assert not strategy.at_target(Dd("0.9"), Dd(1), Dd("0.08"))
    assert strategy.cross_liquidation_move(Dd(3000), Dd(500), Dd(10000)) == Dd("0.25")

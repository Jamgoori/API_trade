from decimal import Decimal as Dd

from hedgebot.rebalance import describe, plan_transfers
from tests.test_backtest import make, params, run_backtest


def test_plan_moves_surplus_to_deficit_by_capital_weights():
    eq = {"arcus": Dd(8900), "hl:io": Dd(3400)}
    w = {"arcus": Dd(7000), "hl:io": Dd(5250)}
    (t,) = plan_transfers(eq, w, Dd(200))
    # pool 12,300 split 4:3 -> io target 5,271.43
    assert (t.source, t.dest, t.amount) == ("arcus", "hl:io", Dd(1871))
    assert "Arcus -> Entropy(io)" in describe([t], eq, w)


def test_small_differences_are_ignored():
    assert plan_transfers({"arcus": Dd(7050), "hl:io": Dd(5200)}, {"arcus": Dd(7000), "hl:io": Dd(5250)}, Dd(200)) == []


def test_backtest_scheduled_rebalance_pulls_venues_back_to_plan():
    drift = lambda i: 100.0 + i * 0.1  # long maker gains on arcus, short hedge loses on hl:xyz
    d = make(n=200, maker_px=drift, hedge_px=drift, notional=6000, direction="long_maker")
    base = dict(capital={"arcus": 5000.0, "hl:xyz": 5000.0}, min_hold_h=1e6, max_hold_h=1e6,
                rebalance_venues=("arcus", "hl:xyz"), rebalance_min_usd=50)
    off = run_backtest([d], params(**base), d.maker.t[0], d.maker.t[-1])
    on = run_backtest([d], params(**base, rebalance_every_h=12), d.maker.t[0], d.maker.t[-1])
    assert on.rebalances >= 1
    gap = lambda r: abs(r.end_equity["arcus"] - r.end_equity["hl:xyz"])
    assert gap(on) < gap(off)

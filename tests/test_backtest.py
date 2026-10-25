from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from hedgebot.backtest.data import PairData, Series
from hedgebot.backtest.sim import BacktestParams, LegSpec, run_backtest
from hedgebot.config import LegConfig, PairConfig

ET = ZoneInfo("America/New_York")
STEP = 900_000  # 15m


def t0(y=2026, mo=10, d=24, h=0):  # a Saturday: all off-hours
    return int(datetime(y, mo, d, h, tzinfo=ET).timestamp() * 1000)


def series(start, n, price_fn):
    t = [start + i * STEP for i in range(n)]
    closes = [price_fn(i) for i in range(n)]
    return Series(t, closes, [c * 1.0005 for c in closes], [c * 0.9995 for c in closes], closes, [0.0] * n)


def make(n=200, maker_px=lambda i: 100.0, hedge_px=lambda i: 100.0, mf=0.0, hf=0.0, notional=5000, direction="auto",
         start=None):
    start = start or t0()
    pair = PairConfig("P", LegConfig("arcus", "A-USD"), LegConfig("hl:xyz", "xyz:A"), Decimal(notional), 5, direction)
    hours = range(start // 3_600_000, (start + n * STEP) // 3_600_000 + 1)
    return PairData(pair, STEP, series(start, n, maker_px), series(start, n, hedge_px),
                    [(h * 3_600_000, mf) for h in hours], [(h * 3_600_000, hf) for h in hours])


def params(**kw):
    specs = {
        ("arcus", "A-USD"): LegSpec("arcus", 0.0, 0.000225, 0.0333, 20, 13.3, False, 1.0, 3.0, 0.005),
        ("hl:xyz", "xyz:A"): LegSpec("hl:xyz", 0.00003, 0.00009, 0.025, 20, 20, False, 1.5, 3.0, 0.01),
    }
    base = dict(capital={"arcus": 5000.0, "hl:xyz": 3000.0}, leg_specs=specs, seed=1)
    base.update(kw)
    return BacktestParams(**base)


def test_flat_market_builds_once_and_pays_only_costs():
    d = make()
    res = run_backtest([d], params(min_hold_h=1e6, max_hold_h=1e6), d.maker.t[0], d.maker.t[-1])
    r = res.pairs["P"]
    assert abs(abs(r.final_qty) * 100 - 5000) < 50
    assert r.rotations == 0 and r.liquidations == 0
    assert res.net_pnl < 0 and abs(res.net_pnl + res.fees + res.slippage) < 1e-6


def test_funding_direction_and_sign():
    # hedge longs pay 0.01%/h, maker funding zero -> long maker / short hedge receives it
    d = make(n=4 * 48, hf=0.0001)
    res = run_backtest([d], params(min_hold_h=1e6, max_hold_h=1e6), d.maker.t[0], d.maker.t[-1])
    assert res.pairs["P"].direction_changes[0][1] == 1
    assert res.funding > 0.0001 * 5000 * 40  # ~47h of carry


def test_off_hours_rotation_generates_maker_volume():
    d = make(n=4 * 40)
    res = run_backtest([d], params(min_hold_h=2, max_hold_h=3, max_daily_cost_usd=1e9), d.maker.t[0], d.maker.t[-1])
    r = res.pairs["P"]
    assert r.rotations >= 8
    assert r.maker_volume > 5 * 5000
    assert r.maker_volume_off_hours == r.maker_volume  # Saturday: everything is off-hours


def test_no_rotation_in_rth_when_window_is_off_hours():
    start = t0(2026, 10, 21, 9)  # Wednesday 09:00-19:00 ET is all inside Arcus RTH
    d = make(n=4 * 10, start=start)
    res = run_backtest([d], params(min_hold_h=1, max_hold_h=1, max_daily_cost_usd=1e9), d.maker.t[0], d.maker.t[-1])
    assert res.pairs["P"].rotations == 0


def test_daily_budget_caps_rotations():
    d = make(n=4 * 40)
    cheap = run_backtest([d], params(min_hold_h=1, max_hold_h=1, max_daily_cost_usd=0.5), d.maker.t[0], d.maker.t[-1])
    rich = run_backtest([d], params(min_hold_h=1, max_hold_h=1, max_daily_cost_usd=1e9), d.maker.t[0], d.maker.t[-1])
    assert cheap.rotations < rich.rotations


def test_gap_against_thin_hedge_account_triggers_risk_events():
    # both legs jump +40%: the short hedge on a thin account blows through maintenance
    jump = lambda i: 100.0 if i < 50 else 140.0
    d = make(n=100, maker_px=jump, hedge_px=jump, notional=12000, direction="long_maker")
    res = run_backtest([d], params(capital={"arcus": 5000.0, "hl:xyz": 3000.0}, allow_transfers=False,
                                   min_hold_h=1e6, max_hold_h=1e6), d.maker.t[0], d.maker.t[-1])
    assert res.stops >= 1 and res.liquidations == 0  # the exchange stop closes it before liquidation
    assert res.min_margin_ratio["hl:xyz"] < 1.7
    no_stop = run_backtest([d], params(capital={"arcus": 5000.0, "hl:xyz": 3000.0}, allow_transfers=False,
                                       min_hold_h=1e6, max_hold_h=1e6, backstop_enabled=False),
                           d.maker.t[0], d.maker.t[-1])
    assert no_stop.liquidations >= 1
    assert res.net_pnl > no_stop.net_pnl  # a stop fill is cheaper than the liquidation penalty


def test_transfers_rebalance_venues_on_slow_drift():
    drift = lambda i: 100.0 + i * 0.1  # +20% over the run, long maker gains, short hedge loses
    d = make(n=200, maker_px=drift, hedge_px=drift, notional=12000, direction="long_maker")
    res = run_backtest([d], params(capital={"arcus": 5000.0, "hl:xyz": 3000.0}, min_hold_h=1e6, max_hold_h=1e6),
                       d.maker.t[0], d.maker.t[-1])
    assert res.transfers >= 1
    assert res.liquidations == 0


def test_isolated_top_up_keeps_a_trending_position_alive():
    from dataclasses import replace as dc_replace
    trend = lambda i: 100.0 + i * 0.25  # +50% over the run against the short hedge
    d = make(n=200, maker_px=trend, hedge_px=trend, notional=4000, direction="long_maker")
    iso = dict(params().leg_specs)
    iso[("hl:xyz", "xyz:A")] = dc_replace(iso[("hl:xyz", "xyz:A")], isolated=True)
    base = dict(capital={"arcus": 6000.0, "hl:xyz": 6000.0}, leg_specs=iso, min_hold_h=1e6, max_hold_h=1e6)
    d.pair.leverage = 2
    with_topup = run_backtest([d], params(**base), d.maker.t[0], d.maker.t[-1])
    without = run_backtest([d], params(**base, isolated_topup_enabled=False), d.maker.t[0], d.maker.t[-1])
    assert with_topup.topups >= 1 and with_topup.stops == 0 and with_topup.liquidations == 0
    assert without.stops + without.derisks + without.liquidations >= 1


def test_pair_level_rotation_off():
    d = make(n=4 * 40)
    d.pair.rotate = False
    res = run_backtest([d], params(min_hold_h=1, max_hold_h=1, max_daily_cost_usd=1e9), d.maker.t[0], d.maker.t[-1])
    assert res.rotations == 0

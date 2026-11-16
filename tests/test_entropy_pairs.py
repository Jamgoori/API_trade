"""Entropy-only books: two different assets on one venue, US-session rotation, drawdown stop, proxy history."""

import random
import textwrap
import time
from datetime import datetime
from decimal import Decimal as Dd
from zoneinfo import ZoneInfo

import pytest

import hedgebot.engine as engine_mod
from hedgebot import strategy
from hedgebot.backtest.data import PairData, Series, proxy_symbol, splice_candles
from hedgebot.backtest.sim import BacktestParams, LegSpec, run_backtest
from hedgebot.config import ConfigError, LegConfig, PairConfig, Secrets, load_config
from hedgebot.engine import DRAWDOWN_STOP_KEY, Engine
from hedgebot.executor import PairExecutor, Phase
from hedgebot.journal import Journal
from hedgebot.models import Candle, Position, Side, Tif
from hedgebot.risk import RiskManager
from tests.conftest import D, FakeContext, FakeDataVenue, make_config, make_market
from tests.test_config_and_risk import FakeEngine, acct

ET = ZoneInfo("America/New_York")


def ms(*args):
    return int(datetime(*args, tzinfo=ET).timestamp() * 1000)


# ------------------------------------------------------------------ config
def write(tmp_path, text):
    p = tmp_path / "c.yaml"
    p.write_text(textwrap.dedent(text))
    return p


def test_example_config_hedges_entropy_on_trade_xyz():
    cfg = load_config("config.example.yaml")
    assert cfg.venues_needed() == ["hl:io", "hl:xyz"]
    for p in cfg.pairs:  # the same stock on both venues, Entropy as the maker
        assert (p.maker.venue, p.hedge.venue) == ("hl:io", "hl:xyz")
        assert p.maker.symbol.split(":")[1] == p.hedge.symbol.split(":")[1] and p.hedge_notional_ratio is None
    symbols = [leg.symbol for p in cfg.pairs for leg in (p.maker, p.hedge)]
    assert len(symbols) == len(set(symbols))  # a market in two pairs would net the positions on the venue
    groups = {}
    for p in cfg.pairs:
        groups.setdefault(p.direction_group, []).append(p.group_side)
    assert all(sorted(sides) == [-1, 1] for sides in groups.values())  # every venue long one, short the other
    assert cfg.strategy.rotate_when == "us_session" and cfg.strategy.rotation_fraction_min == 1
    assert cfg.risk.max_drawdown_pct > cfg.risk.drawdown_warn_pct > 0
    assert cfg.rebalance.enabled and set(cfg.rebalance.venues) == {"hl:io", "hl:xyz"}


def test_two_markets_on_one_venue_need_a_notional_ratio(tmp_path):
    pair = '{name: A, maker: {venue: "hl:io", symbol: "io:A"}, hedge: {venue: "hl:io", symbol: "io:B"}, notional_usd: 100'
    with pytest.raises(ConfigError, match="hedge_notional_ratio"):
        load_config(write(tmp_path, f"hyperliquid: {{dexes: [io]}}\npairs:\n  - {pair}}}\n"))
    cfg = load_config(write(tmp_path, f"hyperliquid: {{dexes: [io]}}\nstrategy: {{rotate_when: us_session}}\n"
                                      f"pairs:\n  - {pair}, hedge_notional_ratio: 0.8}}\n"))
    assert cfg.pairs[0].hedge_notional_ratio == Dd("0.8")
    with pytest.raises(ConfigError, match="drawdown"):
        load_config(write(tmp_path, f"hyperliquid: {{dexes: [io]}}\nrisk: {{drawdown_warn_pct: 20, max_drawdown_pct: 15}}\n"
                                    f"pairs:\n  - {pair}, hedge_notional_ratio: 0.8}}\n"))


def test_us_session_window_and_quantity_ratio():
    assert strategy.rotation_window_open("us_session", False, ms(2026, 11, 17, 11, 0))  # Tuesday 11:00 ET
    assert not strategy.rotation_window_open("us_session", True, ms(2026, 11, 17, 8, 0))  # pre-market
    assert not strategy.rotation_window_open("us_session", True, ms(2026, 11, 21, 11, 0))  # Saturday
    assert not strategy.rotation_window_open("us_session", True, ms(2026, 11, 26, 11, 0))  # Thanksgiving
    assert strategy.qty_ratio(Dd("0.8"), Dd(40), Dd(800)) == Dd("0.04")


# ---------------------------------------------------------------- executor
@pytest.fixture
def io_pair(tmp_path):
    from hedgebot.venues.paper import PaperVenue

    def factory(**strategy_overrides):
        cfg = make_config(**strategy_overrides)
        cfg.capital = {"hl:io": D(20000)}
        cfg.pairs = [PairConfig(name="MEM", maker=LegConfig("hl:io", "io:AAA"), hedge=LegConfig("hl:io", "io:BBB"),
                                notional_usd=D(2000), leverage=5, direction="long_maker",
                                hedge_notional_ratio=D("0.8"))]
        data = FakeDataVenue("hl:io", {
            "io:AAA": make_market("hl:io", "io:AAA", mark_price=D(40), oracle_price=D(40), outside_rth=False),
            "io:BBB": make_market("hl:io", "io:BBB", mark_price=D(800), oracle_price=D(800), outside_rth=False),
        })
        data.set_book("io:AAA", "39.99", "40.01")
        data.set_book("io:BBB", "799.90", "800.10")
        venue = PaperVenue(data, D(20000), state_dir=tmp_path, maker_fill="through", bbo_cache_s=0)
        ctx = FakeContext(cfg, {"hl:io": venue})
        return cfg, ctx, venue, data

    return factory


def fill_maker(ex, data):
    px = ex.clip.order.price  # a long build: the market trades down through our bid
    data.set_book("io:AAA", px - Dd("0.05"), px - Dd("0.03"))


def test_a_two_asset_pair_hedges_by_notional(io_pair):
    cfg, ctx, venue, data = io_pair()
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    now = time.time()
    ex.step(now)
    assert ex.clip is not None and ex.rt.ratio
    assert Dd(ex.rt.ratio) == Dd("0.8") * Dd(40) / Dd(800)  # 0.04 BBB per AAA
    fill_maker(ex, data)
    ex.step(now + 1)
    acct_ = venue.account()
    maker, hedge = acct_.position_size("io:AAA"), acct_.position_size("io:BBB")
    assert maker == 25  # $1,000 clip at ~40
    assert hedge == -1  # $800 of BBB: 0.8 hedge $ per maker $
    assert any(f["purpose"] == "hedge" for f in ctx.fills)


def test_two_asset_pairs_ignore_the_cross_venue_basis_guard(io_pair):
    cfg, ctx, venue, data = io_pair(basis_guard_bps=Dd("0.1"))
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    ex.step(time.time())
    ex.clip = None  # forget the first clip; the relative price now jumps 5%
    data.set_book("io:BBB", "839.90", "840.10")
    ex.step(time.time() + 10)
    assert ex.clip is not None
    assert not [e for e in ctx.events if e[1] == "guard"]


def test_ratio_is_refixed_only_when_flat_and_survives_restarts(io_pair):
    cfg, ctx, venue, data = io_pair()
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    ex.step(time.time())
    assert Dd(ex.rt.ratio) == Dd("0.04")
    venue.place_order("io:AAA", Side.BUY, Dd(25), Dd("40.05"), Tif.IOC)
    venue.place_order("io:BBB", Side.SELL, Dd(1), Dd("799"), Tif.IOC)
    data.set_book("io:BBB", "999.90", "1000.10")
    ex._set_ratio(ex._snapshot(0))
    assert Dd(ex.rt.ratio) == Dd("0.04")  # position on: the ratio it was built with stays
    restarted = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    restarted.restore(ctx.saved["MEM"])
    assert restarted.ratio == Dd("0.04")
    lost_state = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    lost_state._set_ratio(lost_state._snapshot(0))
    assert lost_state.ratio == Dd("0.04")  # read back from the legs (1 / 25)
    venue.place_order("io:AAA", Side.SELL, Dd(25), Dd("39.95"), Tif.IOC, reduce_only=True)
    venue.place_order("io:BBB", Side.BUY, Dd(1), Dd("1001"), Tif.IOC, reduce_only=True)
    ex._set_ratio(ex._snapshot(0))
    assert Dd(ex.rt.ratio) == Dd("0.8") * Dd(40) / Dd(1000)  # flat again: fresh ratio from today's prices


def test_full_rotation_unwinds_to_flat_before_rebuilding(io_pair):
    cfg, ctx, venue, data = io_pair()
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    ex.step(time.time())
    ex.clip = None
    venue.place_order("io:AAA", Side.BUY, Dd(50), Dd("40.05"), Tif.IOC)
    venue.place_order("io:BBB", Side.SELL, Dd(2), Dd("799"), Tif.IOC)
    ex.rt.phase, ex.rt.unwind_to, ex.rt.target_qty = Phase.UNWIND.value, "0", "50"
    snap = ex._snapshot(0)
    assert ex._desired_position(snap, time.time()) == 0  # 50 units left: keep unwinding
    venue.place_order("io:AAA", Side.SELL, Dd(46), Dd("39.95"), Tif.IOC, reduce_only=True)  # $160 left
    assert ex._desired_position(ex._snapshot(0), time.time()) == 0  # inside 8% of target, but not flat
    venue.place_order("io:AAA", Side.SELL, Dd(4), Dd("39.95"), Tif.IOC, reduce_only=True)
    venue.place_order("io:BBB", Side.BUY, Dd(2), Dd("801"), Tif.IOC, reduce_only=True)
    data.set_book("io:BBB", "999.90", "1000.10")
    target = ex._desired_position(ex._snapshot(0), time.time())
    assert ex.rt.phase == Phase.BUILD.value and target == ex.rt.target and target > 0
    assert Dd(ex.rt.ratio) == Dd("0.032")


def test_one_account_carries_both_legs_when_sizing_a_clip(io_pair):
    cfg, ctx, venue, data = io_pair()
    venue.cash = D(300)  # $300 of margin at 5x: $1,500 of gross, i.e. ~$833 of maker + ~$667 of hedge
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    snap = ex._snapshot(0)
    ex._set_ratio(snap)
    room = ex._addable_maker_usd(snap)
    assert Dd(700) < room < Dd(760)  # 300 * 0.9 / (1/5 + 0.8/5) = 750


# ---------------------------------------------------------------- drawdown
class DDEngine(FakeEngine):
    def __init__(self, accounts):
        super().__init__(accounts)
        self.drawdown_stopped = False
        self.stops = []

    def drawdown_stop(self, detail):
        self.drawdown_stopped = True
        self.stops.append(detail)


def io_held(equity, mark, size=100):
    return acct("hl:io", equity, 0, {"io:AAA": Position("io:AAA", Dd(size), Dd(100), Dd(mark))})


def test_drawdown_warns_then_closes_everything():
    cfg = make_config()
    cfg.risk.max_daily_loss_pct = Dd(50)
    cfg.risk.drawdown_warn_pct, cfg.risk.max_drawdown_pct = Dd(5), Dd(10)
    eng = DDEngine({"hl:io": io_held(10000, 100)})
    rm = RiskManager(cfg, eng)
    rm.evaluate()
    eng.accounts["hl:io"] = io_held(10500, 105)  # new high
    rm.evaluate()
    eng.accounts["hl:io"] = io_held(9900, 99)  # 5.7% below the peak
    rm.evaluate()
    assert ("warning", "drawdown") in eng.events and not eng.stops
    eng.accounts["hl:io"] = io_held(9400, 94)  # 10.5% below
    rm.evaluate()
    assert len(eng.stops) == 1 and "10.5%" in eng.stops[0]
    rm.evaluate()
    assert len(eng.stops) == 1


def test_a_withdrawal_is_not_a_drawdown():
    cfg = make_config()
    cfg.risk.max_daily_loss_pct = Dd(50)
    cfg.risk.drawdown_warn_pct, cfg.risk.max_drawdown_pct = Dd(5), Dd(10)
    eng = DDEngine({"hl:io": io_held(10000, 100)})
    rm = RiskManager(cfg, eng)
    rm.evaluate()
    eng.accounts["hl:io"] = io_held(7000, 100)  # $3k withdrawn, positions and prices unchanged
    rm.evaluate()
    assert not eng.stops and rm.hwm == 7000
    eng.accounts["hl:io"] = io_held(6200, 92)  # then a real $800 loss: 11.4% of the new mark
    rm.evaluate()
    assert eng.stops


def test_drawdown_stop_closes_the_pairs_and_persists(tmp_path, monkeypatch):
    cfg = make_config()
    cfg.capital = {"arcus": D(10000), "hl:xyz": D(5000)}
    cfg.journal_path = str(tmp_path / "j.sqlite")
    cfg.paper.state_dir = str(tmp_path / "paper")
    cfg.rebalance.enabled = False
    data = {
        "arcus": FakeDataVenue("arcus", {"TST-USD": make_market("arcus", "TST-USD")}),
        "hl:xyz": FakeDataVenue("hl:xyz", {"xyz:TST": make_market("hl:xyz", "xyz:TST", outside_rth=False)}),
    }
    monkeypatch.setattr(engine_mod, "build_market_venues", lambda c, s, trading: data)
    eng = Engine(cfg, Secrets(), journal=Journal(cfg.journal_path))
    for v in eng.venues.values():
        v.bbo_cache_s = 0
    eng.venues["arcus"].place_order("TST-USD", Side.BUY, Dd(30), Dd("100.05"), Tif.IOC)
    eng.venues["hl:xyz"].place_order("xyz:TST", Side.SELL, Dd(30), Dd("99.95"), Tif.IOC)
    eng.setup()
    eng.drawdown_stop("equity 25.0% below its peak")
    assert eng.account("arcus", 0).position_size("TST-USD") == 0
    assert eng.account("hl:xyz", 0).position_size("xyz:TST") == 0
    assert eng.halted and eng.halt_reason == "drawdown"
    assert eng.journal.load_pair_state(DRAWDOWN_STOP_KEY)["stopped"] is True
    again = Engine(cfg, Secrets(), journal=Journal(cfg.journal_path))
    assert again.drawdown_stopped and again.halted


# ---------------------------------------------------------------- backtest
def test_proxy_history_is_spliced_without_a_jump():
    assert proxy_symbol("io:SKHX", "hl:xyz") == "xyz:SKHX"
    proxy = [Candle(t, D(50), D(51), D(49), D(50)) for t in range(0, 5)]
    main = [Candle(t, D(100), D(101), D(99), D(100)) for t in range(3, 6)]
    out = splice_candles(main, proxy)
    assert [c.t_ms for c in out] == [0, 1, 2, 3, 4, 5]
    assert out[0].close == 100 and out[3] is main[0]


def test_backtest_two_asset_pair_on_one_venue():
    step = 3_600_000
    start = ms(2026, 11, 16, 0, 0)  # Monday
    n = 24 * 8
    t = [start + i * step for i in range(n)]

    def series(px):
        return Series(t, [px] * n, [px * 1.001] * n, [px * 0.999] * n, [px] * n, [0.0] * n)

    pair = PairConfig("MEM", LegConfig("hl:io", "io:AAA"), LegConfig("hl:io", "io:BBB"), Dd(10000), 5, "long_maker",
                      hedge_notional_ratio=Dd("0.8"), min_hold_h=20, max_hold_h=20, rotation_fraction_min=Dd(1),
                      rotation_fraction_max=Dd(1), rotate_when="us_session")
    data = PairData(pair, step, series(40.0), series(800.0), [], [])
    specs = {("hl:io", "io:AAA"): LegSpec("hl:io", 0.00003, 0.00009, 0.02, 25, 25, False, 2.0, 3.0),
             ("hl:io", "io:BBB"): LegSpec("hl:io", 0.00003, 0.00009, 0.033, 15, 15, False, 2.0, 3.0)}
    res = run_backtest([data], BacktestParams(capital={"hl:io": 20000.0}, leg_specs=specs, seed=1), t[0], t[-1])
    r = res.pairs["MEM"]
    assert abs(r.final_qty - 250) < 1e-6  # $10k of AAA at 40
    assert abs(res.venue_avg_oi("hl:io") - 18000) < 2000  # maker $10k + hedge $8k, after the first bar
    assert abs(res.venue_volume["hl:io"] - (r.maker_volume + r.hedge_volume)) < 1e-6
    assert r.hedge_volume / r.maker_volume == pytest.approx(0.8, rel=0.01)
    assert r.rotations >= 3  # 20h holds, rotations only start in US session hours
    assert res.max_drawdown_pct < 1


def test_pair_stats_suggests_the_volatility_ratio():
    from hedgebot.pairstats import pair_stats

    days = list(range(60))
    rng = random.Random(3)
    common = [rng.gauss(0, 0.02) for _ in days]
    maker, hedge, pm, ph = {}, {}, 100.0, 100.0
    for d, c in zip(days, common):
        pm *= 1 + 2 * c  # the maker moves twice as much as the hedge, same direction
        ph *= 1 + c
        maker[d], hedge[d] = pm, ph
    st = pair_stats(maker, hedge, ratio=1.0, days=60)
    assert st.correlation > 0.99
    assert st.suggested_ratio == pytest.approx(2.0, rel=0.05)
    assert st.book_vol_suggested < st.book_vol


# --------------------------------------------------------- direction groups
def test_group_carry_and_flip_rules():
    h = Dd(1) / Dd(8760)  # 1%/yr expressed as an hourly rate, per 1% below
    # side +1 pair: Entropy longs pay 12%/yr more than trade.xyz longs -> long maker loses 12%
    # side -1 pair: Entropy longs pay 6% more -> short maker earns 6%
    members = [(1, Dd(1000), Dd("0.12") * h, Dd(0)), (-1, Dd(1000), Dd("0.06") * h, Dd(0))]
    edge = strategy.group_carry_apr(members)
    assert edge == pytest.approx(Dd("-0.03"), abs=Dd("0.0001"))  # (-12% + 6%) / 2
    assert strategy.choose_group_direction(edge, Dd("0.02")) == -1
    assert strategy.choose_group_direction(Dd("0.01"), Dd("0.02")) == 1
    assert strategy.should_flip_group(1, edge, Dd("0.02"))  # +1 earns -3%, -1 earns +3%: 6% better
    assert not strategy.should_flip_group(-1, edge, Dd("0.02"))


def test_grouped_pairs_need_auto_direction(tmp_path):
    base = ('{name: A, maker: {venue: "hl:io", symbol: "io:A"}, hedge: {venue: "hl:xyz", symbol: "xyz:A"}, '
            'notional_usd: 100, direction_group: g')
    with pytest.raises(ConfigError, match="direction_group"):
        load_config(write(tmp_path, f"pairs:\n  - {base}, direction: long_maker}}\n"))
    with pytest.raises(ConfigError, match="direction_group"):
        load_config(write(tmp_path, f"pairs:\n  - {base}, group_side: 2}}\n"))


@pytest.fixture
def group_engine(tmp_path, monkeypatch):
    def build(f_a_io=Dd(0), f_b_io=Dd(0)):
        cfg = make_config()
        cfg.capital = {"hl:io": D(9000), "hl:xyz": D(9000)}
        cfg.journal_path = str(tmp_path / "j.sqlite")
        cfg.paper.state_dir = str(tmp_path / "paper")
        cfg.rebalance.enabled = False
        cfg.strategy.min_carry_edge_apr = Dd("0.02")
        cfg.strategy.flip_carry_apr = Dd("0.02")
        cfg.pairs = [
            PairConfig("A", LegConfig("hl:io", "io:A"), LegConfig("hl:xyz", "xyz:A"), D(2000), 5, "auto",
                       direction_group="g", group_side=1),
            PairConfig("B", LegConfig("hl:io", "io:B"), LegConfig("hl:xyz", "xyz:B"), D(2000), 5, "auto",
                       direction_group="g", group_side=-1),
        ]
        mk = lambda v, s, f=Dd(0): make_market(v, s, funding_rate_1h=f, outside_rth=False)  # noqa: E731
        data = {
            "hl:io": FakeDataVenue("hl:io", {"io:A": mk("hl:io", "io:A", f_a_io), "io:B": mk("hl:io", "io:B", f_b_io)}),
            "hl:xyz": FakeDataVenue("hl:xyz", {"xyz:A": mk("hl:xyz", "xyz:A"), "xyz:B": mk("hl:xyz", "xyz:B")}),
        }
        monkeypatch.setattr(engine_mod, "build_market_venues", lambda c, s, trading: data)
        eng = Engine(cfg, Secrets(), journal=Journal(cfg.journal_path))
        for v in eng.venues.values():
            v.bbo_cache_s = 0
        eng.setup()
        return eng, data
    return build


def test_group_members_take_opposite_sides_and_follow_funding(group_engine):
    h = Dd(1) / Dd(8760)
    # Entropy longs of A pay 10%/yr, of B 6%/yr more than on trade.xyz: shorting A / buying B on Entropy earns
    eng, data = group_engine(f_a_io=Dd("0.10") * h, f_b_io=Dd("0.06") * h)
    assert eng.group_direction("A") == -1 and eng.group_direction("B") == 1
    assert eng.journal.load_pair_state("__groups__") == {"g": -1}
    restarted = Engine(eng.cfg, Secrets(), journal=Journal(eng.cfg.journal_path))
    assert restarted._group_dir == {"g": -1}


def test_group_turns_together_when_funding_reverses(group_engine):
    eng, data = group_engine()
    assert eng.group_direction("A") == 1  # no edge: +1
    ex_b = next(ex for ex in eng.executors if ex.name == "B")
    ex_b.rt.direction, ex_b.rt.phase, ex_b.rt.next_rotation_ts = -1, "hold", time.time() + 50 * 3600
    h = Dd(1) / Dd(8760)
    data["hl:io"].markets["io:A"] = make_market("hl:io", "io:A", funding_rate_1h=Dd("0.10") * h, outside_rth=False)
    eng._funding_cache.clear()
    assert eng.group_rotation("A") == -1  # A's rotation turns the whole group
    assert eng._group_dir["g"] == -1
    assert ex_b.rt.next_rotation_ts <= time.time()  # B rotates right away instead of in 50h
    assert eng.group_rotation("B") == 1  # B's own rotation now turns it to the group's side


def test_executor_takes_its_direction_from_the_group(io_pair):
    cfg, ctx, venue, data = io_pair()
    cfg.pairs[0].direction, cfg.pairs[0].direction_group, cfg.pairs[0].group_side = "auto", "g", -1
    ctx.groups["dir"] = 1
    ex = PairExecutor(cfg.pairs[0], ctx, random.Random(1))
    ex.step(time.time())
    assert ex.rt.direction == -1 and Dd(ex.rt.target_qty) < 0


def test_backtest_group_keeps_each_venue_long_and_short():
    step = 3_600_000
    start = ms(2026, 11, 16, 0, 0)
    n = 24 * 6
    t = [start + i * step for i in range(n)]

    def series(px):
        return Series(t, [px] * n, [px * 1.001] * n, [px * 0.999] * n, [px] * n, [0.0] * n)

    hours = [(start // step + i) * step for i in range(n)]
    datas = []
    for name, side, f_io in (("A", 1, 0.0), ("B", -1, 0.00001)):  # Entropy longs of B pay ~8.8%/yr extra
        pair = PairConfig(name, LegConfig("hl:io", f"io:{name}"), LegConfig("hl:xyz", f"xyz:{name}"), Dd(5000), 5,
                          "auto", direction_group="g", group_side=side)
        datas.append(PairData(pair, step, series(100.0), series(100.0), [(x, f_io) for x in hours],
                              [(x, 0.0) for x in hours]))
    specs = {}
    for name in ("A", "B"):
        specs[("hl:io", f"io:{name}")] = LegSpec("hl:io", 0.00003, 0.00009, 0.02, 25, 25, False, 2.0, 3.0)
        specs[("hl:xyz", f"xyz:{name}")] = LegSpec("hl:xyz", 0.00003, 0.00009, 0.02, 20, 20, False, 0.3, 0.5)
    res = run_backtest(datas, BacktestParams(capital={"hl:io": 9000.0, "hl:xyz": 9000.0}, leg_specs=specs, seed=1,
                                             min_hold_h=1e6, max_hold_h=1e6, min_carry_edge_apr=0.02),
                       t[0], t[-1])
    a, b = res.pairs["A"], res.pairs["B"]
    assert a.direction_changes[0][1] == 1 and b.direction_changes[0][1] == -1  # group +1: short B on Entropy
    assert a.final_qty > 0 > b.final_qty
    assert res.funding > 0

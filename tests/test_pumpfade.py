"""pumpfade: scanner conditions, simulated fills and the paper position lifecycle against a fake Binance."""

from __future__ import annotations

import pytest

from pumpfade.config import BookSpec, Settings, Strategy
from pumpfade.fills import buy_qty_from_asks, sell_into_bids
from pumpfade.paper import PaperEngine
from pumpfade.signals import DAY_MS, HOUR_MS, DayBar, eligible_symbols, scan, volume_ranks
from pumpfade.store import Store

MIN = 60_000
DAY = 20_000                     # an arbitrary UTC day index
ENTRY = (DAY + 1) * DAY_MS       # 00:00 UTC after the signal day


# ---------------------------------------------------------------- scanner

def test_eligible_symbols_keeps_only_trading_coin_perps():
    info = [
        {"symbol": "FOOUSDT", "contractType": "PERPETUAL", "status": "TRADING", "underlyingType": "COIN"},
        {"symbol": "AAPLUSDT", "contractType": "TRADIFI_PERPETUAL", "status": "TRADING", "underlyingType": "EQUITY"},
        {"symbol": "PAXGUSDT", "contractType": "PERPETUAL", "status": "TRADING", "underlyingType": "COIN",
         "underlyingSubType": ["RWA"]},
        {"symbol": "OLDUSDT", "contractType": "PERPETUAL", "status": "SETTLING", "underlyingType": "COIN"},
        {"symbol": "FOOUSDC", "contractType": "PERPETUAL", "status": "TRADING", "underlyingType": "COIN"},
        {"symbol": "BTCDOMUSDT", "contractType": "PERPETUAL", "status": "TRADING", "underlyingType": "COIN"},
    ]
    assert eligible_symbols(info) == ["FOOUSDT"]


def flat(close, qvol, days=range(DAY - 31, DAY + 1)):
    return {d: DayBar(close, close, close, close, qvol) for d in days}


def universe(change=0.25, high_mult=1.0, majors_move=0.0, target_qvol=1.0):
    """120 liquid fillers, three majors and one small target coin that rose `change` on DAY."""
    bars = {f"F{i:03d}USDT": flat(10, 1e9 + i) for i in range(120)}
    for m in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        b = flat(100, 5e9)
        b[DAY] = DayBar(100, 100, 100, 100 * (1 + majors_move), 5e9)
        bars[m] = b
    t = flat(1.0, target_qvol)
    close = 1.0 * (1 + change)
    t[DAY] = DayBar(1.0, close * high_mult, 1.0, close, target_qvol)
    bars["TGTUSDT"] = t
    return bars


def run(bars, ratio=2.0, funding=(0.0001, 4)):
    return scan(DAY, bars, strategy=Strategy(), ratio_fn=lambda s: ratio, funding_fn=lambda s: funding)


def target(cands):
    return next(c for c in cands if c.symbol == "TGTUSDT")


def test_volume_rank_uses_only_days_before_the_signal_day():
    bars = {"A": flat(1, 10), "B": flat(1, 20)}
    bars["A"][DAY] = DayBar(1, 1, 1, 1, 1e12)  # a huge volume on the signal day itself must not count
    assert volume_ranks(bars, DAY) == {"B": 1, "A": 2}


def test_scan_passes_a_quiet_day_small_cap_pump():
    c = target(run(universe())[0])
    assert c.passed and c.rank > 100 and c.change == pytest.approx(0.25)


@pytest.mark.parametrize("kwargs,ratio,funding,reason", [
    (dict(target_qvol=2e9), 2.0, (0.0, 4), "inside the top 100"),
    (dict(majors_move=0.08), 2.0, (0.0, 4), "majors moved"),
    (dict(majors_move=-0.04), 2.0, (0.0, 4), "majors moved"),
    (dict(high_mult=1.5), 2.0, (0.0, 4), "below the day's high"),
    ({}, 1.5, (0.0, 4), "long/short ratio"),
    ({}, None, (0.0, 4), "no long/short ratio"),
    ({}, 2.0, (-0.01, 4), "funding"),
    ({}, 2.0, (-0.005, 1), "funding"),
])
def test_scan_rejects_with_the_first_failed_filter(kwargs, ratio, funding, reason):
    c = target(run(universe(**kwargs), ratio=ratio, funding=funding)[0])
    assert not c.passed and reason in c.reason


def test_scan_keeps_moderate_negative_funding_and_ignores_small_moves():
    assert target(run(universe(), funding=(-0.004, 1))[0]).passed  # above the 1h floor of −0.5%
    assert target(run(universe(), funding=(-0.009, 4))[0]).passed  # above the 4h floor of −1%
    assert all(c.symbol != "TGTUSDT" for c in run(universe(change=0.19))[0])


# ---------------------------------------------------------------- fills

def test_ioc_sell_stops_at_the_slippage_cap():
    bids = [(100.0, 1.0), (99.8, 1.0), (99.6, 10.0)]
    asks = [(100.2, 1.0)]
    f = sell_into_bids(bids, asks, 1000.0, 0.003)  # floor 99.7: only the first two levels
    assert f.notional == pytest.approx(199.8) and f.qty == pytest.approx(2.0)
    assert not f.exhausted


def test_buy_back_extrapolates_a_thin_book():
    f = buy_qty_from_asks([(99.0, 1.0)], [(100.0, 1.0), (101.0, 1.0)], 3.0)
    assert f.qty == 3.0 and f.vwap == pytest.approx((100 + 101 + 101) / 3) and f.exhausted


# ---------------------------------------------------------------- paper lifecycle

class FakeClient:
    """Price path you script minute by minute; a deep book around the current price."""

    def __init__(self, start_price=1.0):
        self.price = start_price
        self.candles: dict[str, list[list]] = {"TGTUSDT": []}
        self.funding: list[dict] = []
        self.now = ENTRY + MIN

    def add_candle(self, t, o, h, lo, c):
        self.candles["TGTUSDT"].append([t, str(o), str(h), str(lo), str(c), "0", t + MIN - 1, "0"])
        self.price = c

    def klines(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
        if interval == "1h":
            return [[ENTRY, "1.0", "1.0", "1.0", "1.0", "0", ENTRY + HOUR_MS - 1, "0"]]
        rows = [r for r in self.candles[symbol] if (start_ms is None or r[0] >= start_ms)
                and (end_ms is None or r[0] <= end_ms)]
        return rows[:limit]

    def depth(self, symbol, limit=50):
        p = self.price
        return [(p * (1 - 0.0005 * i), 1e6) for i in range(5)], [(p * (1 + 0.0005 * i), 1e6) for i in range(5)]

    def funding_history(self, symbol, start_ms=None, end_ms=None, limit=100):
        return [r for r in self.funding if (start_ms or 0) <= r["fundingTime"] <= (end_ms or 1 << 62)]


@pytest.fixture
def setup(tmp_path):
    client = FakeClient()
    settings = Settings(db_path=tmp_path / "p.sqlite", margin_fraction=0.10,
                        books=(BookSpec("market", "market"), BookSpec("limit", "limit"), BookSpec("half", "half")))
    store = Store(settings.db_path)
    eng = PaperEngine(client, store, settings, notify=lambda *a, **k: None, clock=lambda: client.now)
    cand = target(run(universe())[0])
    return client, store, eng, cand


def legs_of(store, book):
    return store.legs("book=?", (book,))


def test_market_leg_takes_profit_and_limit_leg_falls_back_to_market(setup):
    client, store, eng, cand = setup
    eng.open_signals([cand], DAY)
    m = legs_of(store, "market")[0]
    assert m.status == "open" and m.entry_px == pytest.approx(1.0) and m.notional == pytest.approx(1000)
    assert m.tp_px == pytest.approx(0.85) and m.stop_px == pytest.approx(1.15)
    # the price drifts down; nothing trades through the 1.005 limit within the hour
    for i in range(1, 61):
        t = ENTRY + i * MIN
        client.add_candle(t, 1.0 - i * 0.001, 1.0 - i * 0.001 + 0.001, 1.0 - i * 0.001 - 0.001, 1.0 - i * 0.001)
    client.now = ENTRY + 62 * MIN
    eng.tick()
    lim = legs_of(store, "limit")[0]
    assert lim.kind == "fallback" and lim.status == "open" and lim.entry_px == pytest.approx(client.price, rel=1e-3)
    # then a dump through the take-profit
    t = ENTRY + 62 * MIN
    client.add_candle(t, 0.94, 0.94, 0.80, 0.81)
    client.now = t + 2 * MIN
    eng.tick()
    m = legs_of(store, "market")[0]
    assert m.status == "closed" and m.exit_reason == "take-profit" and m.exit_px == pytest.approx(0.85)
    assert m.pnl == pytest.approx(m.qty * (1.0 - 0.85) - m.fees)
    assert not store.is_banned("market", "TGTUSDT")


def test_limit_fill_then_stop_in_the_same_candle_bans_the_coin(setup):
    client, store, eng, cand = setup
    eng.open_signals([cand], DAY)
    client.add_candle(ENTRY + MIN, 1.0, 1.20, 0.99, 1.18)  # trades through 1.005, then through its 1.15575 stop
    client.add_candle(ENTRY + 2 * MIN, 1.18, 1.20, 1.17, 1.19)  # market legs entered in minute 1 see this one
    client.now = ENTRY + 4 * MIN
    eng.tick()
    lim = legs_of(store, "limit")[0]
    assert lim.kind == "limit" and lim.entry_px == pytest.approx(1.005)
    assert lim.status == "closed" and lim.exit_reason == "stop" and lim.pnl < 0
    assert store.is_banned("limit", "TGTUSDT")
    half = {lg.part: lg for lg in legs_of(store, "half")}
    assert half["A"].exit_reason == "stop" and half["B"].exit_reason == "stop"


def test_stop_wins_when_one_candle_touches_both(setup):
    client, store, eng, cand = setup
    eng.open_signals([cand], DAY)
    client.add_candle(ENTRY + MIN, 1.0, 1.0, 1.0, 1.0)  # the entry minute itself is not used for exits
    client.add_candle(ENTRY + 2 * MIN, 1.0, 1.16, 0.80, 0.9)
    client.now = ENTRY + 4 * MIN
    eng.tick()
    assert legs_of(store, "market")[0].exit_reason == "stop"


def test_time_exit_and_funding(setup):
    client, store, eng, cand = setup
    eng.open_signals([cand], DAY)
    m = legs_of(store, "market")[0]
    client.funding = [{"fundingTime": ENTRY + 4 * HOUR_MS, "fundingRate": "0.001", "markPrice": "1.0"},
                      {"fundingTime": ENTRY + 8 * HOUR_MS, "fundingRate": "-0.002", "markPrice": "1.0"}]
    client.add_candle(ENTRY + MIN, 1.0, 1.01, 0.99, 1.0)
    client.now = m.deadline_ms + MIN
    eng.tick()
    m = legs_of(store, "market")[0]
    assert m.status == "closed" and m.exit_reason == "time"
    assert m.funding == pytest.approx(m.qty * (0.001 - 0.002))


def test_banned_and_held_coins_are_skipped(setup):
    client, store, eng, cand = setup
    store.ban("market", "TGTUSDT", DAY - 5)
    eng.open_signals([cand], DAY)
    assert legs_of(store, "market") == []
    assert len(legs_of(store, "half")) == 2
    eng.open_signals([cand], DAY)  # same coin again while held
    assert len(legs_of(store, "half")) == 2


# ---------------------------------------------------------------- listing pumps

from pumpfade.listings import ListingWatcher  # noqa: E402


class FakeVenue:
    def __init__(self, markets, first=(None, None)):
        self.m = set(markets)
        self.first = first

    def markets(self):
        return set(self.m)

    def first_trade(self, base):
        return self.first


class FakeFutures(FakeClient):
    def __init__(self, onboard_ms):
        super().__init__()
        self.onboard = onboard_ms

    def exchange_info(self):
        return [{"symbol": "TGTUSDT", "contractType": "PERPETUAL", "status": "TRADING", "underlyingType": "COIN",
                 "onboardDate": self.onboard}]


def watcher_setup(tmp_path, onboard_days_ago=100, krw_price=1400.0):
    fut = FakeFutures(onboard_ms=ENTRY - onboard_days_ago * DAY_MS)
    fut.price = 1.0
    settings = Settings(db_path=tmp_path / "l.sqlite", margin_fraction=0.10, books=(BookSpec("limit", "limit"),))
    store = Store(settings.db_path)
    up = FakeVenue({"BTC"})
    w = ListingWatcher(store, fut, settings, venues={"upbit": up}, notify=lambda *a, **k: None, clock=lambda: fut.now)
    eng = PaperEngine(fut, store, settings, notify=lambda *a, **k: None, clock=lambda: fut.now)
    return fut, store, up, w, eng


def test_listing_of_an_existing_perp_is_scheduled_an_hour_after_the_listing_hour(tmp_path):
    fut, store, up, w, eng = watcher_setup(tmp_path)
    w.poll()                                   # first poll only seeds the known markets
    assert store.listings() == []
    start = ENTRY + 2 * HOUR_MS + 25 * MIN     # trading opens at 02:25 UTC
    up.m.add("TGT")
    up.first = (start, 1400.0)                 # 1400 KRW for a $1 coin: same coin
    fut.now = start + 5 * MIN
    w._last_poll.clear()
    w.poll()
    row = store.listing("upbit", "TGT")
    assert row["status"] == "scheduled" and row["perp"] == "TGTUSDT"
    assert row["entry_ms"] == ENTRY + 3 * HOUR_MS
    assert w.due(fut.now) == []
    fut.now = row["entry_ms"] + MIN
    assert [r["base"] for r in w.due(fut.now)] == ["TGT"]
    eng.open_listing("TGTUSDT", "upbit", row["entry_ms"])
    leg = store.legs()[0]
    assert leg.sleeve == "listing" and leg.status == "open" and leg.stop_px == pytest.approx(leg.entry_px * 1.15)


def test_new_tokens_and_ticker_clashes_are_skipped(tmp_path):
    fut, store, up, w, eng = watcher_setup(tmp_path, onboard_days_ago=10)
    w.poll()
    up.m.add("TGT")
    up.first = (fut.now, 1400.0)
    w._last_poll.clear()
    w.poll()
    assert "new token" in store.listing("upbit", "TGT")["reason"]

    fut, store, up, w, eng = watcher_setup(tmp_path / "b")
    w.poll()
    up.m.add("TGT")
    up.first = (fut.now, 3.0)                  # 3 KRW for a $1 coin: a different coin with the same ticker
    w._last_poll.clear()
    w.poll()
    assert "price mismatch" in store.listing("upbit", "TGT")["reason"]


def test_a_listing_stop_does_not_drop_the_coin(tmp_path):
    fut, store, up, w, eng = watcher_setup(tmp_path)
    eng.open_listing("TGTUSDT", "upbit", fut.now)
    fut.add_candle(fut.now // MIN * MIN + MIN, 1.0, 1.2, 1.0, 1.2)
    fut.now += 3 * MIN
    eng.tick()
    assert store.legs()[0].exit_reason == "stop"
    assert not store.is_banned("limit", "TGTUSDT")


def test_a_failed_scan_is_retried_until_the_deadline(tmp_path):
    from pumpfade.runner import Runner

    s = Settings(db_path=tmp_path / "r.sqlite", watch_listings=False)
    now = [ENTRY - 10 * MIN]
    sent = []

    class Notes:
        def send(self, msg, **kw):
            sent.append(msg)

    class Idle:
        def tick(self):
            pass

        def settle_all_funding(self):
            pass

    r = Runner(s, client=object(), store=Store(s.db_path), notifier=Notes(), clock=lambda: now[0])
    r.engine = Idle()
    r.step()                                   # started before the close: waits for it
    calls = []

    def flaky(day):
        calls.append(day)
        if len(calls) == 1:
            raise ConnectionError("wifi dropped")
        return "scanned"

    r.scan_and_enter = flaky
    now[0] = ENTRY + 61_000
    r.step()
    assert r.store.get("last_scan_day") == DAY - 1 and "retrying" in sent[-1]
    now[0] += 30_000
    r.step()
    assert calls == [DAY, DAY] and r.store.get("last_scan_day") == DAY and sent[-1] == "scanned"
    now[0] += 30_000
    r.step()
    assert len(calls) == 2                     # done for the day


@pytest.mark.parametrize("ratio, passed", [(1.5, False), (1.51, True), (3.0, True), (3.01, False), (4.2, False)])
def test_long_short_ratio_window(ratio, passed):
    c = target(run(universe(), ratio=ratio)[0])
    assert c.passed is passed
    if ratio > 3.0:
        assert "too crowded" in c.reason

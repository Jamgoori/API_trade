"""Shared fakes: an in-memory market data venue and an executor context."""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from hedgebot.config import Config, LegConfig, PairConfig
from hedgebot.models import BBO, AccountState, Candle, MarketInfo, OrderBook, OrderState
from hedgebot.util import D, ZERO, quantize_to_step
from hedgebot.venues.base import Venue, VenueError


def make_market(venue: str, symbol: str, **kw) -> MarketInfo:
    base = dict(
        venue=venue, symbol=symbol, step_size=D("0.001"), min_notional=D(10), max_leverage=D(10),
        off_hours_max_leverage=D("6.67"), maintenance_margin_fraction=D("0.05"), maker_fee=D(0),
        taker_fee=D("0.0002"), is_rwa=True, outside_rth=True, isolated_only=False, online=True,
        funding_rate_1h=D(0), mark_price=D(100), oracle_price=D(100),
    )
    base.update(kw)
    return MarketInfo(**base)


class FakeDataVenue(Venue):
    """Market data you can move by hand."""

    def __init__(self, name: str, markets: dict[str, MarketInfo]):
        self.name = name
        self.markets = markets
        self.books: dict[str, OrderBook] = {}
        self.fail_bbo = False
        for s, m in markets.items():
            self.set_book(s, m.mark_price - D("0.01"), m.mark_price + D("0.01"))

    def set_book(self, symbol: str, bid, ask, size="1000", depth: int = 5, tick="0.01"):
        bid, ask, size, tick = D(bid), D(ask), D(size), D(tick)
        bids = [(bid - tick * i, size) for i in range(depth)]
        asks = [(ask + tick * i, size) for i in range(depth)]
        self.books[symbol] = OrderBook(bids, asks, time.time())

    def set_empty(self, symbol: str, side: str):
        book = self.books[symbol]
        if side == "asks":
            self.books[symbol] = OrderBook(book.bids, [], time.time())
        else:
            self.books[symbol] = OrderBook([], book.asks, time.time())

    def refresh_markets(self):
        return self.markets

    def market(self, symbol):
        try:
            return self.markets[symbol]
        except KeyError:
            raise VenueError(f"unknown {symbol}") from None

    def bbo(self, symbol):
        if self.fail_bbo:
            raise VenueError("bbo down", retryable=True)
        book = self.books[symbol]
        if not book.bids or not book.asks:
            # keep a quote on the empty side far away so the executor can still read a mid
            bid = book.bids[0] if book.bids else (book.asks[0][0] - D(1), D(0))
            ask = book.asks[0] if book.asks else (book.bids[0][0] + D(1), D(0))
            return BBO(bid[0], bid[1], ask[0], ask[1], time.time())
        b = book.bbo()
        return BBO(b.bid, b.bid_size, b.ask, b.ask_size, time.time())

    def book(self, symbol, depth=20):
        b = self.books[symbol]
        return OrderBook(b.bids[:depth], b.asks[:depth], time.time())

    def round_price(self, symbol, price, mode="nearest"):
        return quantize_to_step(D(price), D("0.01"), mode)

    def round_size(self, symbol, size):
        return quantize_to_step(abs(D(size)), self.markets[symbol].step_size, "down")

    def tick_size(self, symbol, price):
        return D("0.01")

    def funding_history(self, symbol, start_ms, end_ms):
        return []

    def candles(self, symbol, interval, start_ms, end_ms):
        return []


class FakeContext:
    """Minimal engine stand-in for executor tests."""

    def __init__(self, cfg: Config, venues: dict[str, Venue]):
        self.cfg = cfg
        self.venues = venues
        self.halted = False
        self.forced: dict[str, Decimal] = {}
        self.events: list[tuple[str, str, str]] = []
        self.fills: list[dict] = []
        self.orders: list[tuple] = []
        self.saved: dict[str, dict] = {}
        self.budget_ok = True
        self.funding: dict[tuple[str, str], Decimal] = {}

    def account(self, venue, max_age_s=5.0) -> AccountState:
        return self.venues[venue].account()

    def invalidate_account(self, venue):
        pass

    def take_forced_reduction(self, pair):
        return self.forced.pop(pair, None)

    def rotation_budget_left(self):
        return self.budget_ok

    def funding_avg(self, venue, symbol):
        return self.funding.get((venue, symbol), ZERO)

    def maker_venue_bias(self, pair, venue):
        return ZERO

    def journal_fill(self, **kw):
        self.fills.append(kw)

    def journal_order(self, venue, pair, purpose, order: OrderState):
        self.orders.append((venue, purpose, order.side.value, order.size, order.tif.value, order.status.value))

    def save_pair_state(self, pair, data):
        self.saved[pair] = data

    def event(self, level, kind, message, **kw):
        self.events.append((level, kind, message))

    def kinds(self) -> list[str]:
        return [k for _, k, _ in self.events]


def make_config(**strategy_overrides) -> Config:
    cfg = Config(mode="paper")
    cfg.capital = {"arcus": D(10000), "hl:xyz": D(5000)}
    cfg.pairs = [PairConfig(name="T", maker=LegConfig("arcus", "TST-USD"), hedge=LegConfig("hl:xyz", "xyz:TST"),
                            notional_usd=D(3000), leverage=5, direction="long_maker")]
    cfg.strategy.clip_usd_min = D(1000)
    cfg.strategy.clip_usd_max = D(1000)
    cfg.strategy.session_blackout_min = 0
    cfg.strategy.seed = 1
    for k, v in strategy_overrides.items():
        setattr(cfg.strategy, k, v)
    cfg.execution.imbalance_grace_s = 0
    cfg.execution.ioc_confirm_timeout_s = 0.1
    return cfg


@pytest.fixture
def setup_pair(tmp_path):
    from hedgebot.venues.paper import PaperVenue

    def factory(**strategy_overrides):
        cfg = make_config(**strategy_overrides)
        arcus_data = FakeDataVenue("arcus", {"TST-USD": make_market("arcus", "TST-USD")})
        hl_data = FakeDataVenue("hl:xyz", {"xyz:TST": make_market("hl:xyz", "xyz:TST", taker_fee=D("0.00009"),
                                                                  outside_rth=False)})
        venues = {
            "arcus": PaperVenue(arcus_data, D(10000), state_dir=tmp_path, maker_fill="through", bbo_cache_s=0),
            "hl:xyz": PaperVenue(hl_data, D(5000), state_dir=tmp_path, maker_fill="through", bbo_cache_s=0),
        }
        ctx = FakeContext(cfg, venues)
        return cfg, ctx, venues, arcus_data, hl_data

    return factory

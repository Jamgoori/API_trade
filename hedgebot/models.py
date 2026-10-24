"""Venue-neutral data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from .util import ZERO


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY

    @staticmethod
    def for_delta(delta: Decimal) -> "Side":
        """Side that moves a position by a signed delta."""
        return Side.BUY if delta > 0 else Side.SELL


class Tif(str, Enum):
    ALO = "ALO"  # post-only (maker)
    IOC = "IOC"  # immediate-or-cancel (taker)
    GTT = "GTT"  # resting good-til-time


class OrderStatus(str, Enum):
    PENDING = "PENDING"  # accepted, state not known yet (Arcus is asynchronous)
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"

    @property
    def done(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED)


@dataclass(frozen=True)
class MarketInfo:
    venue: str
    symbol: str
    step_size: Decimal
    min_notional: Decimal
    max_leverage: Decimal
    off_hours_max_leverage: Decimal
    maintenance_margin_fraction: Decimal
    maker_fee: Decimal  # fraction of notional, e.g. 0.000225
    taker_fee: Decimal
    is_rwa: bool
    outside_rth: bool
    isolated_only: bool
    online: bool
    funding_rate_1h: Decimal  # current hourly rate, positive = longs pay
    mark_price: Decimal
    oracle_price: Decimal
    volume_24h_usd: Decimal = ZERO
    open_interest_usd: Decimal = ZERO
    min_size: Decimal = ZERO
    max_order_size: Decimal | None = None
    extra: dict = field(default_factory=dict, compare=False, hash=False)

    def max_leverage_for_new_risk(self) -> Decimal:
        """Arcus raises initial margin off-hours for orders that add exposure."""
        return self.off_hours_max_leverage if self.outside_rth else self.max_leverage


@dataclass(frozen=True)
class BBO:
    bid: Decimal
    bid_size: Decimal
    ask: Decimal
    ask_size: Decimal
    ts: float  # local receive time, epoch seconds

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        mid = self.mid
        return (self.ask - self.bid) / mid * 10000 if mid else Decimal("Infinity")

    def price_for(self, side: Side, aggressive: bool) -> Decimal:
        """Touch price: passive joins our own side, aggressive takes the other side."""
        if side is Side.BUY:
            return self.ask if aggressive else self.bid
        return self.bid if aggressive else self.ask


@dataclass(frozen=True)
class OrderBook:
    bids: list[tuple[Decimal, Decimal]]
    asks: list[tuple[Decimal, Decimal]]
    ts: float

    def bbo(self) -> BBO:
        if not self.bids or not self.asks:
            raise ValueError("empty side in order book")
        return BBO(self.bids[0][0], self.bids[0][1], self.asks[0][0], self.asks[0][1], self.ts)


@dataclass
class OrderState:
    order_id: str
    symbol: str
    side: Side
    price: Decimal
    size: Decimal
    tif: Tif
    status: OrderStatus
    filled: Decimal = ZERO
    avg_price: Decimal | None = None
    client_id: str | None = None
    reduce_only: bool = False
    reject_reason: str | None = None
    fee: Decimal | None = None  # actual fee when the venue reports it
    created_ts: float = 0.0

    @property
    def remaining(self) -> Decimal:
        return max(ZERO, self.size - self.filled)


@dataclass
class Position:
    symbol: str
    size: Decimal  # signed base units, + long / - short
    entry_price: Decimal
    mark_price: Decimal
    liquidation_price: Decimal | None = None
    margin_mode: str = "cross"
    margin_used: Decimal = ZERO
    unrealized_pnl: Decimal = ZERO

    @property
    def notional(self) -> Decimal:
        return abs(self.size) * self.mark_price


@dataclass
class AccountState:
    venue: str
    equity: Decimal
    free_collateral: Decimal
    maintenance_margin: Decimal
    positions: dict[str, Position]
    ts: float

    def position_size(self, symbol: str) -> Decimal:
        pos = self.positions.get(symbol)
        return pos.size if pos else ZERO

    @property
    def gross_notional(self) -> Decimal:
        return sum((p.notional for p in self.positions.values()), ZERO)

    @property
    def margin_ratio(self) -> Decimal | None:
        """equity / maintenance requirement; liquidation happens at 1.0. None when flat."""
        if self.maintenance_margin <= 0:
            return None
        return self.equity / self.maintenance_margin


@dataclass(frozen=True)
class Fill:
    venue: str
    symbol: str
    order_id: str
    side: Side
    size: Decimal
    price: Decimal
    fee: Decimal
    liquidity: str  # maker | taker
    ts_ms: int
    fill_id: str = ""


@dataclass(frozen=True)
class Candle:
    t_ms: int  # bar open time
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume_usd: Decimal = ZERO

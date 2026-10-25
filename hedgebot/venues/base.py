"""Venue interface plus the HTTP plumbing the concrete venues share."""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from decimal import Decimal

import requests

from ..models import BBO, AccountState, Candle, Fill, MarketInfo, OrderBook, OrderState, Side, Tif

log = logging.getLogger(__name__)


class VenueError(Exception):
    """A request the venue refused or that could not be completed."""

    def __init__(self, message: str, *, status: int | None = None, payload=None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.payload = payload
        self.retryable = retryable


class WeightLimiter:
    """Client-side token bucket so polling never trips the venue's per-IP limit."""

    def __init__(self, capacity: float, refill_per_s: float):
        self.capacity = capacity
        self.refill_per_s = refill_per_s
        self.tokens = capacity
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, weight: float) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill_per_s)
                self.updated = now
                if self.tokens >= weight:
                    self.tokens -= weight
                    return
                wait = (weight - self.tokens) / self.refill_per_s
            time.sleep(min(wait, 2.0))


class HttpClient:
    """requests.Session with timeouts, bounded retries on transient errors and 429 handling."""

    def __init__(self, base_url: str, *, timeout: float = 10.0, limiter: WeightLimiter | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.limiter = limiter
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "hedgebot/0.1"

    def request(self, method: str, path: str, *, weight: float = 1, retries: int = 2, **kwargs):
        url = self.base_url + path
        for attempt in range(retries + 1):
            if self.limiter:
                self.limiter.acquire(weight)
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                if attempt < retries:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise VenueError(f"{method} {path}: {exc}", retryable=True) from exc
            if resp.status_code == 429 and attempt < retries:
                wait = float(resp.headers.get("Retry-After", "1"))
                log.warning("429 from %s, backing off %.1fs", path, wait)
                time.sleep(min(wait, 10.0))
                continue
            if resp.status_code >= 500 and attempt < retries and method == "GET":
                time.sleep(0.5 * (attempt + 1))
                continue
            return resp
        raise VenueError(f"{method} {path}: retries exhausted", retryable=True)

    @staticmethod
    def json_or_raise(resp, context: str):
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        if resp.status_code >= 400:
            raise VenueError(
                f"{context}: HTTP {resp.status_code} {body}",
                status=resp.status_code,
                payload=body,
                retryable=resp.status_code in (429, 500, 502, 503, 504),
            )
        return body


class Venue(ABC):
    """Everything the executor, risk manager and paper simulator need from an exchange.

    One instance = one margin account. Hyperliquid HIP-3 dexes keep separate margin per
    dex, so `hl:xyz` and `hl:io` are two venues sharing one wallet.
    """

    name: str
    can_trade: bool = False

    # ---- market data ----
    @abstractmethod
    def refresh_markets(self) -> dict[str, MarketInfo]: ...

    @abstractmethod
    def market(self, symbol: str) -> MarketInfo: ...

    @abstractmethod
    def bbo(self, symbol: str) -> BBO: ...

    @abstractmethod
    def book(self, symbol: str, depth: int = 20) -> OrderBook: ...

    @abstractmethod
    def round_price(self, symbol: str, price: Decimal, mode: str = "nearest") -> Decimal: ...

    @abstractmethod
    def round_size(self, symbol: str, size: Decimal) -> Decimal: ...

    @abstractmethod
    def tick_size(self, symbol: str, price: Decimal) -> Decimal:
        """Price increment that applies at `price`."""

    @abstractmethod
    def funding_history(self, symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, Decimal]]: ...

    @abstractmethod
    def candles(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[Candle]: ...

    # ---- account / trading ----
    def account(self) -> AccountState:
        raise VenueError(f"{self.name}: account access needs credentials")

    def place_order(
        self,
        symbol: str,
        side: Side,
        size: Decimal,
        price: Decimal,
        tif: Tif,
        *,
        reduce_only: bool = False,
        client_id: str | None = None,
    ) -> OrderState:
        raise VenueError(f"{self.name}: trading needs credentials")

    def cancel_order(self, symbol: str, order_id: str) -> None:
        raise VenueError(f"{self.name}: trading needs credentials")

    def order_state(self, order: OrderState) -> OrderState:
        raise VenueError(f"{self.name}: trading needs credentials")

    def cancel_all(self) -> None:
        raise VenueError(f"{self.name}: trading needs credentials")

    def set_leverage(self, symbol: str, leverage: int, cross: bool) -> None:
        raise VenueError(f"{self.name}: trading needs credentials")

    def place_stop(self, symbol: str, side: Side, size: Decimal, trigger: Decimal) -> OrderState:
        """Reduce-only stop-market that rests on the venue: the backstop when the bot is not running.

        It is canceled with `cancel_order`. Venues may close the whole position regardless of `size`.
        """
        raise VenueError(f"{self.name}: stop orders need credentials")

    def heartbeat(self) -> None:
        """Refresh any server-side dead man's switch. No-op where none is used."""

    def disarm_heartbeat(self) -> None:
        """Disarm the dead man's switch on clean shutdown."""

    def recent_fills(self, since_ms: int) -> list[Fill]:
        return []

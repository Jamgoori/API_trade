"""Exchange plumbing check with real orders at minimum size (`hedgebot selftest`).

Per venue, on one symbol from the config:
  1. read the account and set leverage / margin mode
  2. rest a post-only buy ~3% under the bid, read it back from the exchange
     (price and size must match what we sent), cancel it
  3. with --fill: buy the minimum size with IOC, place an exchange stop, cancel it,
     add $1 of isolated margin (isolated markets), then close with a reduce-only IOC

Run it with the bot stopped. Nothing here touches positions the bot already holds:
a symbol that already has a position is skipped.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable

from .config import Config
from .models import OrderState, OrderStatus, Side, Tif
from .util import D, ZERO, fmt_decimal, quantize_to_step
from .venues.base import Venue, VenueError

RESTING_DISCOUNT = D("0.03")
IOC_SLIPPAGE = D("0.003")
MIN_TEST_USD = D(12)  # Hyperliquid rejects orders under $10


@dataclass
class VenueReport:
    venue: str
    symbol: str
    steps: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.steps)

    def add(self, step: str, ok: bool, detail: str = "") -> bool:
        self.steps.append((step, ok, detail))
        return ok


def pick_symbols(cfg: Config, only: list[str] | None = None) -> dict[str, tuple[str, int, bool]]:
    """venue -> (symbol, leverage, cross) from the first enabled pair leg on that venue."""
    picked: dict[str, tuple[str, int, bool]] = {}
    for pair in cfg.pairs:
        if not pair.enabled:
            continue
        for leg in (pair.maker, pair.hedge):
            if leg.venue in picked or (only and leg.venue not in only):
                continue
            mode = cfg.arcus.margin_mode if leg.venue == "arcus" else cfg.hyperliquid.margin_mode
            picked[leg.venue] = (leg.symbol, int(D(pair.leverage)), mode == "cross")
    return picked


def test_size(venue: Venue, symbol: str, price: Decimal, usd: Decimal) -> Decimal:
    m = venue.market(symbol)
    usd = max(usd, m.min_notional * D("1.2"), MIN_TEST_USD)
    size = quantize_to_step(usd / price, m.step_size, "up")
    return max(size, quantize_to_step(m.min_size, m.step_size, "up")) if m.min_size else size


def readback(venue: Venue, order: OrderState) -> tuple[Decimal, Decimal] | None:
    """(price, size) as the exchange recorded the order, or None if the venue cannot say."""
    from .venues.arcus import ArcusVenue
    from .venues.hyperliquid import HyperliquidVenue

    if isinstance(venue, ArcusVenue):
        signer = venue.signer
        resp = venue.http.request("GET", f"/v1/order/{order.order_id}", weight=2,
                                  params={"address": signer.address, "accountIndex": signer.account_index})
        body = venue.http.json_or_raise(resp, f"GET /v1/order/{order.order_id}")
        return D(body["price"]), D(body["originalSize"])
    if isinstance(venue, HyperliquidVenue):
        body = venue.client.info({"type": "orderStatus", "user": venue.client.account_address,
                                  "oid": int(order.order_id)}, 2)
        o = (body.get("order") or {}).get("order") or {}
        return D(o["limitPx"]), D(o["origSz"])
    return None


def wait(venue: Venue, order: OrderState, done: Callable[[OrderState], bool], timeout_s: float,
         poll_s: float = 0.5) -> OrderState:
    deadline = time.time() + timeout_s
    while True:
        order = venue.order_state(order)
        if done(order) or time.time() >= deadline:
            return order
        time.sleep(poll_s)


def position_size(venue: Venue, symbol: str) -> Decimal:
    pos = venue.account().positions.get(symbol)
    return pos.size if pos else ZERO


def check_venue(venue: Venue, symbol: str, leverage: int, cross: bool, *, fill: bool, usd: Decimal,
                timeout_s: float = 20, poll_s: float = 0.5) -> VenueReport:
    rep = VenueReport(venue.name, symbol)
    resting: OrderState | None = None
    opening = False
    bought = ZERO
    try:
        m = venue.market(symbol)
        acct = venue.account()
        if not rep.add("account", True, f"equity ${acct.equity:,.2f}, free ${acct.free_collateral:,.2f}"):
            return rep
        if symbol in acct.positions:
            rep.add("existing position", False, f"{symbol} already has {acct.positions[symbol].size}; skipped")
            return rep
        lev = int(min(D(leverage), m.max_leverage))
        mode = "isolated" if (m.isolated_only or not cross) else "cross"
        venue.set_leverage(symbol, lev, cross=(mode == "cross"))
        rep.add("set leverage", True, f"{lev}x {mode}")


        bbo = venue.bbo(symbol)
        price = venue.round_price(symbol, bbo.bid * (1 - RESTING_DISCOUNT), "down")
        lower = m.extra.get("lowerTradingBound") if m.outside_rth else None
        if lower and price <= D(lower):
            price = venue.round_price(symbol, D(lower) * D("1.002"), "up")
        size = test_size(venue, symbol, price, usd)
        resting = venue.place_order(symbol, Side.BUY, size, price, Tif.ALO)
        if resting.status is OrderStatus.REJECTED:
            rep.add("resting order", False, f"rejected: {resting.reject_reason}")
            return rep
        resting = wait(venue, resting, lambda o: o.status is not OrderStatus.PENDING, timeout_s, poll_s)
        if not rep.add("resting order", resting.status is OrderStatus.OPEN,
                       f"buy {fmt_decimal(size)} @ {fmt_decimal(price)} -> {resting.status.value}"
                       + (f" ({resting.reject_reason})" if resting.reject_reason else "")):
            return rep
        got = readback(venue, resting)
        if got is not None:
            match = got[0] == price and got[1] == size
            if not rep.add("exchange readback", match, f"exchange has {fmt_decimal(got[1])} @ {fmt_decimal(got[0])}"):
                return rep
        venue.cancel_order(symbol, resting.order_id)
        resting = wait(venue, resting, lambda o: o.status.done, timeout_s, poll_s)
        if not rep.add("cancel", resting.status is OrderStatus.CANCELED, resting.status.value):
            return rep
        resting = None

        if not fill:
            return rep
        bbo = venue.bbo(symbol)
        price = venue.round_price(symbol, bbo.ask * (1 + IOC_SLIPPAGE), "up")
        size = test_size(venue, symbol, bbo.ask, usd)
        opening = True
        buy = venue.place_order(symbol, Side.BUY, size, price, Tif.IOC)
        buy = wait(venue, buy, lambda o: o.status.done, timeout_s, poll_s)
        bought = buy.filled
        opened = settle_position(venue, symbol, buy.filled, timeout_s, poll_s)
        if not rep.add("open (IOC)", opened > 0, f"filled {fmt_decimal(buy.filled)}, position {fmt_decimal(opened)}"
                       + (f" ({buy.reject_reason})" if buy.reject_reason else "")):
            return rep

        trigger = venue.round_price(symbol, (venue.market(symbol).mark_price or bbo.bid) * D("0.85"), "down")
        stop = venue.place_stop(symbol, Side.SELL, opened, trigger)
        if rep.add("exchange stop", stop.status is not OrderStatus.REJECTED and bool(stop.order_id),
                   f"sell @ trigger {fmt_decimal(trigger)} -> {stop.status.value}"
                   + (f" ({stop.reject_reason})" if stop.reject_reason else "")):
            venue.cancel_order(symbol, stop.order_id)
            stop = wait(venue, stop, lambda o: o.status.done, timeout_s, poll_s)
            rep.add("cancel stop", stop.status is OrderStatus.CANCELED, stop.status.value)

        pos = venue.account().positions.get(symbol)
        if pos is not None and pos.margin_mode == "isolated":
            before = pos.margin_used
            venue.add_isolated_margin(symbol, D(1))
            after = venue.account().positions[symbol].margin_used
            rep.add("add isolated margin", after > before, f"${before:,.2f} -> ${after:,.2f}")
    except VenueError as exc:
        rep.add("error", False, str(exc))
    finally:
        if resting is not None and resting.order_id and not resting.status.done:
            try:
                venue.cancel_order(symbol, resting.order_id)
            except VenueError as exc:
                rep.add("cleanup cancel", False, f"cancel order {resting.order_id} by hand: {exc}")
        if opening:
            try:
                _close(venue, symbol, rep, timeout_s, poll_s, expected=bought)
            except VenueError as exc:
                rep.add("close (reduce-only IOC)", False, f"close {symbol} by hand: {exc}")
    return rep


def settle_position(venue: Venue, symbol: str, expected: Decimal, timeout_s: float, poll_s: float) -> Decimal:
    """Position size once it reflects `expected` filled units (account stores lag fills), or the last read."""
    deadline = time.time() + timeout_s
    while True:
        size = position_size(venue, symbol)
        if abs(size) >= expected or time.time() >= deadline:
            return size
        time.sleep(poll_s)


def _close(venue: Venue, symbol: str, rep: VenueReport, timeout_s: float, poll_s: float,
           expected: Decimal = ZERO) -> None:
    for attempt in range(3):
        size = settle_position(venue, symbol, expected, timeout_s, poll_s) if attempt == 0 else position_size(venue, symbol)
        if size == 0:
            rep.add("close (reduce-only IOC)", True, "flat")
            return
        side = Side.SELL if size > 0 else Side.BUY
        bbo = venue.bbo(symbol)
        ref = bbo.bid if side is Side.SELL else bbo.ask
        price = venue.round_price(symbol, ref * (1 - IOC_SLIPPAGE if side is Side.SELL else 1 + IOC_SLIPPAGE),
                                  "down" if side is Side.SELL else "up")
        order = venue.place_order(symbol, side, abs(size), price, Tif.IOC, reduce_only=True)
        wait(venue, order, lambda o: o.status.done, timeout_s, poll_s)
    left = position_size(venue, symbol)
    rep.add("close (reduce-only IOC)", left == 0, "flat" if left == 0 else f"{left} still open: close it by hand")


def format_reports(reports: list[VenueReport]) -> str:
    lines = []
    for r in reports:
        lines.append(f"{r.venue} {r.symbol}: {'OK' if r.ok else 'FAILED'}")
        for step, ok, detail in r.steps:
            lines.append(f"  [{'ok' if ok else '!!'}] {step:<22} {detail}")
    return "\n".join(lines)

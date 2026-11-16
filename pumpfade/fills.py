"""Simulated fills against a live order book snapshot."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Fill:
    notional: float      # USDT actually filled
    qty: float
    vwap: float
    mid: float
    slippage: float      # cost against the mid, as a positive fraction
    exhausted: bool = False  # the book ran out before the order was filled


def sell_into_bids(bids, asks, notional: float, max_slip: float | None) -> Fill:
    """Market sell of `notional` USDT. With max_slip, only levels within that fraction of the best bid fill (IOC)."""
    mid = (bids[0][0] + asks[0][0]) / 2
    floor = bids[0][0] * (1 - max_slip) if max_slip is not None else 0.0
    left, qty, cost = notional, 0.0, 0.0
    for price, size in bids:
        if price < floor or left <= 0:
            break
        take = min(price * size, left)
        qty += take / price
        cost += take
        left -= take
    exhausted = left > 1e-9 and max_slip is None
    if exhausted:  # book too thin: assume the rest fills at the last level seen
        price = bids[-1][0]
        qty += left / price
        cost += left
    vwap = cost / qty if qty else bids[0][0]
    return Fill(cost, qty, vwap, mid, (mid - vwap) / mid, exhausted)


def buy_qty_from_asks(bids, asks, qty: float) -> Fill:
    """Market buy of `qty` coins (closing a short). Always fills fully; a thin book is extrapolated."""
    mid = (bids[0][0] + asks[0][0]) / 2
    left, got, cost = qty, 0.0, 0.0
    for price, size in asks:
        if left <= 0:
            break
        take = min(size, left)
        got += take
        cost += take * price
        left -= take
    exhausted = left > 1e-12
    if exhausted:
        got += left
        cost += left * asks[-1][0]
    vwap = cost / got if got else asks[0][0]
    return Fill(cost, got, vwap, mid, (vwap - mid) / mid, exhausted)

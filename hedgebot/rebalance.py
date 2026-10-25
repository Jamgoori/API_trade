"""Venue rebalancing plan: how much to move where to get back to the planned capital split.

The bot never moves money itself (its keys cannot withdraw); it tells you the transfer.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .util import ZERO, D


@dataclass(frozen=True)
class Transfer:
    source: str
    dest: str
    amount: Decimal


def plan_transfers(equity: dict[str, Decimal], weights: dict[str, Decimal], min_transfer: Decimal) -> list[Transfer]:
    """Pool the venues' equity, split it by `weights`, and match surpluses to deficits."""
    total_w = sum(weights.values(), ZERO)
    pool = sum((equity[v] for v in weights), ZERO)
    if total_w <= 0 or pool <= 0:
        return []
    delta = {v: pool * weights[v] / total_w - equity[v] for v in weights}  # + needs money
    surplus = sorted(((-d, v) for v, d in delta.items() if d < 0), reverse=True)
    deficit = sorted(((d, v) for v, d in delta.items() if d > 0), reverse=True)
    out: list[Transfer] = []
    si = di = 0
    while si < len(surplus) and di < len(deficit):
        give, src = surplus[si]
        need, dst = deficit[di]
        amount = min(give, need)
        if amount >= min_transfer:
            out.append(Transfer(src, dst, amount.quantize(Decimal(1))))
        surplus[si] = (give - amount, src)
        deficit[di] = (need - amount, dst)
        if surplus[si][0] <= 0:
            si += 1
        if deficit[di][0] <= 0:
            di += 1
    return out


def describe(transfers: list[Transfer], equity: dict[str, Decimal], weights: dict[str, Decimal]) -> str:
    names = {"arcus": "Arcus", "hl:io": "Entropy(io)", "hl:xyz": "trade.xyz"}
    if not transfers:
        return "rebalance: balances are within plan, nothing to move (" + ", ".join(
            f"{names.get(v, v)} ${equity[v]:,.0f}" for v in weights) + ")"
    after = dict(equity)
    for t in transfers:
        after[t.source] -= t.amount
        after[t.dest] += t.amount
    lines = ["rebalance: " + "; ".join(f"move ${t.amount:,.0f} {names.get(t.source, t.source)} -> "
                                       f"{names.get(t.dest, t.dest)}" for t in transfers)]
    lines += [f"  {names.get(v, v)}: ${equity[v]:,.0f} -> ${after[v]:,.0f}" for v in weights]
    return "\n".join(lines)


def weights_from(capital: dict[str, Decimal], venues: list[str]) -> dict[str, Decimal]:
    return {v: D(capital[v]) for v in venues}

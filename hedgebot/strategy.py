"""Pure decision functions shared by the live executor and the backtest.

Keeping these side-effect free means the backtest exercises the same rules the bot
trades with, and they can be unit tested without any exchange.

Sign conventions:
- `direction` is the sign of the maker (Arcus) leg: +1 = long maker / short hedge.
- funding rates are hourly, positive = longs pay shorts (both venues).
- basis is (maker_price - hedge_price) / hedge_price in bps.
"""

from __future__ import annotations

import random
from decimal import Decimal

from .models import Side
from .util import HOURS_PER_YEAR, ZERO, D, bps, quantize_to_step


def carry_apr(direction: int, f_maker_1h: Decimal, f_hedge_1h: Decimal) -> Decimal:
    """Annualised funding received per $1 of notional for a maker-leg direction."""
    return direction * (D(f_hedge_1h) - D(f_maker_1h)) * HOURS_PER_YEAR


def choose_direction(preference: str, f_maker_1h: Decimal, f_hedge_1h: Decimal, min_edge_apr: Decimal,
                     venue_bias_usd: Decimal) -> int:
    """Pick the maker-leg direction for a fresh build.

    Funding decides when the carry difference is meaningful. Otherwise the pair leans
    against the bot's existing directional book on the maker venue (`venue_bias_usd`
    = sum of signed maker notionals of the other pairs) so that a market-wide move
    does not hit every Arcus position the same way under cross margin.
    """
    if preference == "long_maker":
        return 1
    if preference == "short_maker":
        return -1
    edge = carry_apr(1, f_maker_1h, f_hedge_1h)
    if abs(edge) >= min_edge_apr:
        return 1 if edge > 0 else -1
    return -1 if venue_bias_usd > 0 else 1


def should_flip(direction: int, f_maker_1h: Decimal, f_hedge_1h: Decimal, flip_apr: Decimal) -> bool:
    """Flip only when the opposite side earns at least `flip_apr` more carry."""
    current = carry_apr(direction, f_maker_1h, f_hedge_1h)
    return (-current) - current >= flip_apr


def basis_bps(maker_price: Decimal, hedge_price: Decimal) -> Decimal:
    return bps(D(maker_price), D(hedge_price))


def basis_edge_bps(maker_side: Side, maker_price: Decimal, hedge_price: Decimal, reference_bps: Decimal) -> Decimal:
    """How much better than the reference basis a maker/hedge execution is (positive = better).

    Buying the maker leg (and selling the hedge) is good when the maker is cheap
    relative to the hedge; selling it is good when the maker is rich.
    """
    b = basis_bps(maker_price, hedge_price)
    return (reference_bps - b) if maker_side is Side.BUY else (b - reference_bps)


def ewma(previous: Decimal | None, value: Decimal, dt_s: float, half_life_s: float) -> Decimal:
    if previous is None or half_life_s <= 0:
        return value
    alpha = D(1 - 0.5 ** (max(dt_s, 0.0) / half_life_s))
    return previous + alpha * (value - previous)


def target_quantity(direction: int, notional_usd: Decimal, price: Decimal, step: Decimal) -> Decimal:
    if price <= 0:
        return ZERO
    qty = quantize_to_step(D(notional_usd) / D(price), step, "down")
    return qty * direction


def clip_usd(gap_usd: Decimal, clip_min: Decimal, clip_max: Decimal, rng: random.Random) -> Decimal:
    """Randomised clip size that never leaves a remainder smaller than clip_min."""
    gap_usd = abs(D(gap_usd))
    if gap_usd <= clip_max:
        return gap_usd
    if gap_usd < clip_max + clip_min:
        return gap_usd / 2
    return D(round(rng.uniform(float(clip_min), float(clip_max)), 2))


def next_rotation_delay_s(min_h: float, max_h: float, rng: random.Random) -> float:
    return rng.uniform(min_h, max_h) * 3600


def rotation_fraction(fmin: Decimal, fmax: Decimal, rng: random.Random) -> Decimal:
    return D(round(rng.uniform(float(fmin), float(fmax)), 3))


def rotation_window_open(rotate_when: str, maker_outside_rth: bool) -> bool:
    return rotate_when == "any" or maker_outside_rth


def at_target(position: Decimal, target: Decimal, tolerance: Decimal) -> bool:
    if target == 0:
        return position == 0
    return abs(position / target - 1) <= tolerance


def addable_notional(equity: Decimal, gross_notional: Decimal, free_collateral: Decimal,
                     leverage: Decimal, max_account_leverage: Decimal, buffer: Decimal = Decimal("0.9")) -> Decimal:
    """New notional a margin account can carry without breaching IM or the account leverage cap."""
    by_margin = free_collateral * leverage * buffer
    by_account = equity * max_account_leverage - gross_notional
    return max(ZERO, min(by_margin, by_account))


def cross_liquidation_move(equity: Decimal, maintenance: Decimal, gross_notional: Decimal) -> Decimal | None:
    """Uniform adverse move (fraction) that takes a cross account to its maintenance requirement."""
    if gross_notional <= 0:
        return None
    return max(ZERO, (equity - maintenance) / gross_notional)

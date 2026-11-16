"""How well the two legs of a pair hedge each other, from daily closes.

For a pair of different assets (hedge_notional_ratio) the hedge is statistical: it holds while the
two prices move together. This measures, over a trailing window:

- correlation of daily returns,
- the volatility ratio maker / hedge (= hedge $ per maker $ that makes both legs move by the same
  dollar amount, which is what `hedge_notional_ratio` should be),
- the annualised volatility of the hedged book per $1 of gross notional, at the configured ratio
  and at the suggested one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DAY_MS = 86_400_000


@dataclass
class PairStats:
    days: int
    correlation: float
    maker_vol: float  # annualised
    hedge_vol: float
    suggested_ratio: float  # hedge $ per maker $ (volatility ratio)
    book_vol: float  # annualised, per $1 of gross notional, at the ratio given
    book_vol_suggested: float


def daily_closes(t: list[int], close: list[float]) -> dict[int, float]:
    """Last close of each UTC day."""
    out: dict[int, float] = {}
    for ts, c in zip(t, close):
        out[ts // DAY_MS] = c
    return out


def _std(xs: list[float]) -> float:
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _book_vol(rm: list[float], rh: list[float], ratio: float) -> float:
    """Long $1 maker / short $ratio hedge, normalised to $1 gross."""
    book = [(a - ratio * b) / (1 + ratio) for a, b in zip(rm, rh)]
    return _std(book) * math.sqrt(365)


def pair_stats(maker: dict[int, float], hedge: dict[int, float], ratio: float, days: int) -> PairStats | None:
    common = sorted(set(maker) & set(hedge))[-(days + 1):]
    if len(common) < 10:
        return None
    rm = [math.log(maker[b] / maker[a]) for a, b in zip(common, common[1:])]
    rh = [math.log(hedge[b] / hedge[a]) for a, b in zip(common, common[1:])]
    sm, sh = _std(rm), _std(rh)
    if sm == 0 or sh == 0:
        return None
    mean_m, mean_h = sum(rm) / len(rm), sum(rh) / len(rh)
    cov = sum((a - mean_m) * (b - mean_h) for a, b in zip(rm, rh)) / (len(rm) - 1)
    suggested = sm / sh
    return PairStats(len(rm), cov / (sm * sh), sm * math.sqrt(365), sh * math.sqrt(365), suggested,
                     _book_vol(rm, rh, ratio), _book_vol(rm, rh, suggested))

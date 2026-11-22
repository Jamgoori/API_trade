"""Daily scan: which coins qualify for a short at the 00:00 UTC (09:00 KST) open.

Mirrors the research backtest (research/STRATEGY.md): the day that just closed must be +20% or more against the
previous close, the coin must rank outside the top 100 by trailing 30-day volume (days before the signal day
only), the majors' average move that day must be within −3%..+7%, the close must be at most 30% below the day's
high, the global long/short account ratio must be above 1.5 and the last funding must not be deeply negative.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Callable

from .config import Strategy

log = logging.getLogger(__name__)

HOUR_MS = 3_600_000
DAY_MS = 86_400_000

# Old index perps and stablecoins that report underlyingType COIN.
NON_COIN = {"BTCDOMUSDT", "DEFIUSDT", "BLUEBIRDUSDT", "FOOTBALLUSDT", "USDCUSDT", "TUSDUSDT", "BUSDUSDT"}


def eligible_symbols(exchange_info: list[dict]) -> list[str]:
    """Trading USDT perpetuals on crypto coins: no equities, commodities, FX, pre-IPO, indices, RWA or stablecoins."""
    out = []
    for s in exchange_info:
        sym = s["symbol"]
        if not re.fullmatch(r"[A-Z0-9]+USDT", sym) or sym in NON_COIN:
            continue
        if s.get("contractType") != "PERPETUAL" or s.get("status") != "TRADING":
            continue
        sub = s.get("underlyingSubType") or []
        if s.get("underlyingType") != "COIN" or "RWA" in sub or "Stablecoin" in sub:
            continue
        out.append(sym)
    return sorted(out)


@dataclass
class DayBar:
    open: float
    high: float
    low: float
    close: float
    qvol: float


def parse_daily(rows: list[list]) -> dict[int, DayBar]:
    """Binance kline rows → {UTC day index: bar}."""
    return {int(r[0]) // DAY_MS: DayBar(float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[7])) for r in rows}


def volume_ranks(bars: dict[str, dict[int, DayBar]], day: int, days: int = 30) -> dict[str, int]:
    """Rank by average daily quote volume over the `days` days before `day` (1 = most traded)."""
    avg = {}
    for sym, b in bars.items():
        vals = [b[d].qvol for d in range(day - days, day) if d in b]
        if vals:
            avg[sym] = sum(vals) / len(vals)
    return {s: i + 1 for i, s in enumerate(sorted(avg, key=lambda s: -avg[s]))}


@dataclass
class Candidate:
    symbol: str
    day: int                      # UTC day index of the +20% day; entry is at the open of day + 1
    change: float
    rank: int | None
    retrace: float
    majors: float
    ratio: float | None = None
    funding: float | None = None
    funding_interval_h: int | None = None
    passed: bool = False
    reason: str = ""

    def as_row(self) -> dict:
        return asdict(self)


def scan(day: int, bars: dict[str, dict[int, DayBar]], *, strategy: Strategy,
         ratio_fn: Callable[[str], float | None],
         funding_fn: Callable[[str], tuple[float, int] | None]) -> tuple[list[Candidate], float]:
    """Every coin up `trigger` or more on `day`, each with the first filter it failed (or passed=True).

    ratio_fn / funding_fn are only called for coins that passed the cheaper price filters.
    Returns the candidates and the majors' average move that day.
    """
    st = strategy
    moves = []
    for m in st.majors:
        b = bars.get(m, {})
        if day in b and day - 1 in b:
            moves.append(b[day].close / b[day - 1].close - 1)
    majors = sum(moves) / len(moves) if moves else float("nan")
    majors_ok = len(moves) == len(st.majors) and st.majors_min <= majors <= st.majors_max
    ranks = volume_ranks(bars, day, st.rank_days)

    out = []
    for sym in sorted(bars):
        b = bars[sym]
        if day not in b or day - 1 not in b or b[day - 1].close <= 0:
            continue
        change = b[day].close / b[day - 1].close - 1
        if change < st.trigger:
            continue
        c = Candidate(sym, day, change, ranks.get(sym), 1 - b[day].close / b[day].high, majors)
        if c.rank is None:
            c.reason = "no volume history before the signal day"
        elif c.rank <= st.min_rank:
            c.reason = f"volume rank {c.rank} is inside the top {st.min_rank}"
        elif not majors_ok:
            c.reason = f"majors moved {majors:+.1%}, outside {st.majors_min:+.0%}..{st.majors_max:+.0%}"
        elif c.retrace > st.max_retrace:
            c.reason = f"closed {c.retrace:.0%} below the day's high"
        else:
            c.ratio = ratio_fn(sym)
            if c.ratio is None:
                c.reason = "no long/short ratio snapshot"
            elif c.ratio <= st.min_ratio:
                c.reason = f"long/short ratio {c.ratio:.2f} <= {st.min_ratio}"
            elif c.ratio > st.max_ratio:
                c.reason = f"long/short ratio {c.ratio:.2f} > {st.max_ratio} (too crowded)"
            else:
                f = funding_fn(sym)
                if f is not None:
                    c.funding, c.funding_interval_h = f
                    floor = st.funding_floor_1h if c.funding_interval_h < 4 else st.funding_floor_long
                    if c.funding <= floor:
                        c.reason = f"funding {c.funding:+.3%} per {c.funding_interval_h}h <= {floor:+.1%}"
                if not c.reason:
                    c.passed = True
        out.append(c)
    return out, majors


def load_daily_bars(client, symbols: list[str], day: int, days: int = 30, workers: int = 8) -> dict[str, dict[int, DayBar]]:
    """Daily bars up to and including `day` for every symbol (enough history for the volume rank)."""
    end_ms = (day + 1) * DAY_MS - 1

    def one(sym):
        try:
            return sym, {d: b for d, b in parse_daily(client.klines(sym, "1d", end_ms=end_ms, limit=days + 3)).items()
                         if d <= day}
        except Exception as exc:  # one bad symbol must not stop the scan
            log.warning("daily klines failed for %s: %s", sym, exc)
            return sym, {}

    with ThreadPoolExecutor(workers) as ex:
        return {s: b for s, b in ex.map(one, symbols) if b}


def make_ratio_fn(client, entry_ms: int) -> Callable[[str], float | None]:
    def fn(sym: str) -> float | None:
        got = client.long_short_ratio(sym, entry_ms - 1)
        if got is None:
            return None
        ratio, ts = got
        return ratio if entry_ms - ts <= 30 * 60_000 else None
    return fn


def make_funding_fn(client, entry_ms: int, intervals: dict[str, int]) -> Callable[[str], tuple[float, int] | None]:
    """Last funding settled at or just after the entry hour (the 00:00 UTC settlement counts)."""
    def fn(sym: str) -> tuple[float, int] | None:
        rows = client.funding_history(sym, end_ms=entry_ms + 60_000, limit=5)
        rows = [r for r in rows if int(r["fundingTime"]) <= entry_ms + 60_000]
        if not rows:
            return None
        return float(rows[-1]["fundingRate"]), intervals.get(sym, 8)
    return fn


def run_scan(client, strategy: Strategy, day: int) -> tuple[list[Candidate], float]:
    """Full scan for the UTC day `day` against live Binance data (also works for recent past days)."""
    symbols = eligible_symbols(client.exchange_info())
    bars = load_daily_bars(client, symbols, day, strategy.rank_days)
    entry_ms = (day + 1) * DAY_MS
    intervals = client.funding_intervals()
    return scan(day, bars, strategy=strategy, ratio_fn=make_ratio_fn(client, entry_ms),
                funding_fn=make_funding_fn(client, entry_ms, intervals))

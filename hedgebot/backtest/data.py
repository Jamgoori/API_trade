"""Historical candles and funding for both legs of a pair, cached on disk."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import PairConfig
from ..models import Candle
from ..venues.base import Venue

log = logging.getLogger(__name__)

CACHE_MAX_AGE_S = 6 * 3600


@dataclass
class Series:
    """Bars as plain floats keyed by bar open time (ms)."""

    t: list[int]
    open: list[float]
    high: list[float]
    low: list[float]
    close: list[float]
    volume_usd: list[float]

    @classmethod
    def from_candles(cls, candles: list[Candle]) -> "Series":
        return cls([c.t_ms for c in candles], [float(c.open) for c in candles], [float(c.high) for c in candles],
                    [float(c.low) for c in candles], [float(c.close) for c in candles],
                    [float(c.volume_usd) for c in candles])

    def __len__(self) -> int:
        return len(self.t)


@dataclass
class PairData:
    pair: PairConfig
    interval_ms: int
    maker: Series
    hedge: Series
    maker_funding: list[tuple[int, float]]  # (ts_ms, hourly rate)
    hedge_funding: list[tuple[int, float]]
    proxy_bars: tuple[int, int] = (0, 0)  # bars taken from a proxy venue (maker, hedge)


def _cache_path(cache_dir: Path, kind: str, venue: str, symbol: str, interval: str, start_ms: int, end_ms: int) -> Path:
    safe = f"{kind}_{venue}_{symbol}_{interval}_{start_ms // 3_600_000}_{end_ms // 3_600_000}".replace(":", "-")
    return cache_dir / f"{safe}.json"


def _cached(path: Path, fetch):
    if path.exists() and time.time() - path.stat().st_mtime < CACHE_MAX_AGE_S:
        return json.loads(path.read_text())
    data = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return data


def _fetch_candles(venue: Venue, symbol: str, interval: str, start_ms: int, end_ms: int, cache_dir: Path) -> list[Candle]:
    path = _cache_path(cache_dir, "candles", venue.name, symbol, interval, start_ms, end_ms)
    rows = _cached(path, lambda: [[c.t_ms, str(c.open), str(c.high), str(c.low), str(c.close), str(c.volume_usd)]
                                  for c in venue.candles(symbol, interval, start_ms, end_ms)])
    from decimal import Decimal
    return [Candle(int(r[0]), Decimal(r[1]), Decimal(r[2]), Decimal(r[3]), Decimal(r[4]), Decimal(r[5])) for r in rows]


def _fetch_funding(venue: Venue, symbol: str, start_ms: int, end_ms: int, cache_dir: Path) -> list[tuple[int, float]]:
    path = _cache_path(cache_dir, "funding", venue.name, symbol, "1h", start_ms, end_ms)
    rows = _cached(path, lambda: [[t, str(r)] for t, r in venue.funding_history(symbol, start_ms, end_ms)])
    return [(int(t), float(r)) for t, r in rows]


def proxy_symbol(symbol: str, proxy_venue: str) -> str:
    """io:SKHX -> xyz:SKHX for an hl:xyz proxy (HIP-3 symbols carry their dex prefix)."""
    base = symbol.split(":", 1)[-1]
    return f"{proxy_venue[3:]}:{base}" if proxy_venue.startswith("hl:") else base


def splice_candles(main: list[Candle], proxy: list[Candle]) -> list[Candle]:
    """Proxy bars before the first main bar, rescaled so the series joins without a jump."""
    if not main or not proxy:
        return main or proxy
    first = main[0].t_ms
    before = [c for c in proxy if c.t_ms < first]
    if not before:
        return main
    join = next((c for c in proxy if c.t_ms >= first), before[-1])
    if join.open <= 0:
        return main
    k = main[0].open / join.open
    scaled = [Candle(c.t_ms, c.open * k, c.high * k, c.low * k, c.close * k, c.volume_usd) for c in before]
    return scaled + main


def _leg(venue: Venue, symbol: str, interval: str, start_ms: int, end_ms: int, cache: Path,
         proxy: Venue | None) -> tuple[list[Candle], list[tuple[int, float]], int]:
    candles = _fetch_candles(venue, symbol, interval, start_ms, end_ms, cache)
    funding = _fetch_funding(venue, symbol, start_ms, end_ms, cache)
    added = 0
    if proxy is not None:
        psym = proxy_symbol(symbol, proxy.name)
        try:
            pc = _fetch_candles(proxy, psym, interval, start_ms, end_ms, cache)
            pf = _fetch_funding(proxy, psym, start_ms, end_ms, cache)
        except Exception as exc:  # proxy market missing: keep the venue's own (shorter) history
            log.warning("no proxy history for %s on %s: %s", psym, proxy.name, exc)
            return candles, funding, 0
        spliced = splice_candles(candles, pc)
        added = len(spliced) - len(candles)
        candles = spliced
        first_f = funding[0][0] if funding else end_ms + 1
        funding = [r for r in pf if r[0] < first_f] + funding
    return candles, funding, added


def load_pair_data(pair: PairConfig, venues: dict[str, Venue], interval: str, interval_ms: int, start_ms: int,
                   end_ms: int, cache_dir: str | Path, proxies: dict[str, Venue] | None = None) -> PairData:
    """Candles and funding of both legs. `proxies` maps a venue to another venue listing the same stocks
    (e.g. hl:io -> hl:xyz): its bars fill the time before the venue listed the market."""
    cache = Path(cache_dir)
    proxies = proxies or {}
    maker_v, hedge_v = venues[pair.maker.venue], venues[pair.hedge.venue]
    log.info("loading %s: %s %s / %s %s", pair.name, maker_v.name, pair.maker.symbol, hedge_v.name, pair.hedge.symbol)
    mc, mf, m_added = _leg(maker_v, pair.maker.symbol, interval, start_ms, end_ms, cache, proxies.get(pair.maker.venue))
    hc, hf, h_added = _leg(hedge_v, pair.hedge.symbol, interval, start_ms, end_ms, cache, proxies.get(pair.hedge.venue))
    data = PairData(pair, interval_ms, Series.from_candles(mc), Series.from_candles(hc), mf, hf)
    data.proxy_bars = (m_added, h_added)
    return data

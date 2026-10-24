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


def load_pair_data(pair: PairConfig, venues: dict[str, Venue], interval: str, interval_ms: int, start_ms: int,
                   end_ms: int, cache_dir: str | Path) -> PairData:
    cache = Path(cache_dir)
    maker_v, hedge_v = venues[pair.maker.venue], venues[pair.hedge.venue]
    log.info("loading %s: %s %s / %s %s", pair.name, maker_v.name, pair.maker.symbol, hedge_v.name, pair.hedge.symbol)
    maker = Series.from_candles(_fetch_candles(maker_v, pair.maker.symbol, interval, start_ms, end_ms, cache))
    hedge = Series.from_candles(_fetch_candles(hedge_v, pair.hedge.symbol, interval, start_ms, end_ms, cache))
    mf = _fetch_funding(maker_v, pair.maker.symbol, start_ms, end_ms, cache)
    hf = _fetch_funding(hedge_v, pair.hedge.symbol, start_ms, end_ms, cache)
    return PairData(pair, interval_ms, maker, hedge, mf, hf)

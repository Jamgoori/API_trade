"""Listing-pump watcher (research/STRATEGY.md rule 2).

Every poll compares the markets of Upbit KRW, Bithumb KRW and Binance spot USDT with what was seen before.
A new market for a coin whose Binance USDT perp has traded for 30+ days becomes a listing event; once the market
has its first trade, a short on the perp is scheduled for the start of the listing hour plus one hour.
The first poll only records the markets that already exist.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from hedgebot.venues.base import HttpClient, WeightLimiter

from .config import Settings
from .signals import DAY_MS, HOUR_MS, eligible_symbols

log = logging.getLogger(__name__)
MIN_MS = 60_000


class KrwVenue:
    """Upbit and Bithumb share the same quotation API shape."""

    def __init__(self, name: str, base_url: str):
        self.name = name
        self.http = HttpClient(base_url, timeout=10, limiter=WeightLimiter(8, 8))
        self.http.session.headers["User-Agent"] = "pumpfade/0.1"

    def _get(self, path, params=None):
        return HttpClient.json_or_raise(self.http.request("GET", path, params=params), f"{self.name} {path}")

    def markets(self) -> set[str]:
        return {m["market"].split("-", 1)[1] for m in self._get("/v1/market/all") if m["market"].startswith("KRW-")}

    def first_trade(self, base: str) -> tuple[int | None, float | None]:
        """(first 1-minute candle time in ms, last price in KRW); (None, None) before the first trade."""
        rows = self._get("/v1/candles/minutes/1", {"market": f"KRW-{base}", "count": 200})
        if not isinstance(rows, list) or not rows:
            return None, None
        first = min(int(datetime.fromisoformat(r["candle_date_time_utc"]).replace(tzinfo=timezone.utc).timestamp() * 1000)
                    for r in rows)
        return first, float(rows[0]["trade_price"])


class SpotVenue:
    """Binance spot USDT pairs."""

    name = "binance_spot"

    def __init__(self, base_url: str):
        self.http = HttpClient(base_url, timeout=15, limiter=WeightLimiter(600, 20))
        self.http.session.headers["User-Agent"] = "pumpfade/0.1"

    def _get(self, path, params=None, weight=1):
        return HttpClient.json_or_raise(self.http.request("GET", path, params=params, weight=weight), f"spot {path}")

    def markets(self) -> set[str]:
        info = self._get("/api/v3/exchangeInfo", {"permissions": "SPOT"}, weight=20)["symbols"]
        return {s["baseAsset"] for s in info if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"}

    def first_trade(self, base: str) -> tuple[int | None, float | None]:
        rows = self._get("/api/v3/klines", {"symbol": f"{base}USDT", "interval": "1m", "startTime": 0, "limit": 1})
        if not rows:
            return None, None
        px = float(self._get("/api/v3/ticker/price", {"symbol": f"{base}USDT"})["price"])
        return int(rows[0][0]), px


class ListingWatcher:
    def __init__(self, store, futures_client, settings: Settings, venues: dict | None = None, notify=None, clock=None):
        self.store = store
        self.fut = futures_client
        self.st = settings.strategy
        self.venues = venues if venues is not None else {
            "upbit": KrwVenue("upbit", settings.upbit_url),
            "bithumb": KrwVenue("bithumb", settings.bithumb_url),
            "binance_spot": SpotVenue(settings.spot_url),
        }
        self.notify = notify or (lambda msg, **kw: log.info(msg))
        self.clock = clock or (lambda: int(time.time() * 1000))
        self._perps: dict[str, int] = {}
        self._perps_ms = 0
        self._last_poll: dict[str, int] = {}

    def perps(self) -> dict[str, int]:
        """Trading coin perps → onboard time (ms), refreshed hourly."""
        now = self.clock()
        if now - self._perps_ms > HOUR_MS or not self._perps:
            info = self.fut.exchange_info()
            ok = set(eligible_symbols(info))
            self._perps = {s["symbol"]: int(s.get("onboardDate") or 0) for s in info if s["symbol"] in ok}
            self._perps_ms = now
        return self._perps

    def poll(self) -> None:
        now = self.clock()
        for name, venue in self.venues.items():
            every = 5 * MIN_MS if name == "binance_spot" else MIN_MS
            if now - self._last_poll.get(name, 0) < every:
                continue
            self._last_poll[name] = now
            try:
                current = venue.markets()
            except Exception as exc:
                log.warning("%s market list failed: %s", name, exc)
                continue
            known = self.store.get(f"known_{name}")
            if known is None:  # first run: remember what exists, raise nothing
                self.store.put(f"known_{name}", sorted(current))
                continue
            for base in sorted(current - set(known)):
                self._on_new(name, base, now)
            self.store.put(f"known_{name}", sorted(set(known) | current))
        for row in self.store.listings("waiting"):
            self._check_start(row, now)

    def _perp_for(self, base: str) -> str | None:
        perps = self.perps()
        for sym in (f"{base}USDT", f"1000{base}USDT", f"1000000{base}USDT"):
            if sym in perps:
                return sym
        return None

    def _on_new(self, venue: str, base: str, now: int) -> None:
        if self.store.listing(venue, base):
            return
        perp = self._perp_for(base)
        row = {"venue": venue, "base": base, "perp": perp, "detected_ms": now, "status": "waiting", "reason": ""}
        if perp is None:
            row.update(status="skipped", reason="no Binance USDT perp")
        elif now - self.perps()[perp] < self.st.listing_min_perp_age_days * DAY_MS:
            row.update(status="skipped", reason=f"perp younger than {self.st.listing_min_perp_age_days} days (new token)")
        self.store.save_listing(row)
        log.info("new %s market %s: %s %s", venue, base, row["status"], row["reason"])
        if row["status"] == "waiting":
            self._check_start(row, now)

    def _check_start(self, row: dict, now: int) -> None:
        venue = self.venues[row["venue"]]
        try:
            start, venue_px = venue.first_trade(row["base"])
        except Exception as exc:
            log.warning("first trade lookup failed for %s %s: %s", row["venue"], row["base"], exc)
            return
        if start is None:
            if now - row["detected_ms"] > 2 * DAY_MS:
                row.update(status="skipped", reason="never started trading")
                self.store.save_listing(row)
            return
        row["start_ms"] = start
        if now - start > self.st.listing_max_detect_delay_hours * HOUR_MS:
            row.update(status="skipped", reason="seen too long after the listing opened")
            self.store.save_listing(row)
            return
        mismatch = self._price_mismatch(row, venue_px)
        if mismatch:
            row.update(status="skipped", reason=mismatch)
            self.store.save_listing(row)
            return
        row["entry_ms"] = start // HOUR_MS * HOUR_MS + self.st.listing_entry_delay_hours * HOUR_MS
        row["status"] = "scheduled"
        self.store.save_listing(row)
        self.notify(f"listing: {row['base']} opened on {row['venue']}; short {row['perp']} scheduled at "
                    f"{time.strftime('%H:%M', time.gmtime(row['entry_ms'] / 1000))} UTC")

    def _price_mismatch(self, row: dict, venue_px: float | None) -> str:
        """Guard against a different coin with the same ticker."""
        if not venue_px:
            return ""
        bids, asks = self.fut.depth(row["perp"], 5)
        perp_px = (bids[0][0] + asks[0][0]) / 2
        mult = 1_000_000 if row["perp"].startswith("1000000") else 1000 if row["perp"].startswith("1000") else 1
        coin_usd = perp_px / mult
        if row["venue"] == "binance_spot":
            ok = abs(venue_px / coin_usd - 1) < 0.10
        else:
            lo, hi = self.st.usdkrw_range
            ok = lo <= venue_px / coin_usd <= hi
        return "" if ok else f"price mismatch ({row['venue']} {venue_px:.6g} vs perp {coin_usd:.6g})"

    def due(self, now: int) -> list[dict]:
        return [r for r in self.store.listings("scheduled") if r["entry_ms"] <= now]

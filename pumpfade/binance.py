"""Binance USD-M futures public endpoints (no API key)."""

from __future__ import annotations

from hedgebot.venues.base import HttpClient, VenueError, WeightLimiter

__all__ = ["BinancePublic", "VenueError"]


class BinancePublic:
    def __init__(self, base_url: str = "https://fapi.binance.com", *, timeout: float = 10.0):
        # Binance allows 2400 request weight per minute per IP; stay well under it.
        self.http = HttpClient(base_url, timeout=timeout, limiter=WeightLimiter(600, 25))
        self.http.session.headers["User-Agent"] = "pumpfade/0.1"

    def _get(self, path: str, params: dict | None = None, weight: float = 1):
        resp = self.http.request("GET", path, weight=weight, params=params)
        return HttpClient.json_or_raise(resp, path)

    def exchange_info(self) -> list[dict]:
        return self._get("/fapi/v1/exchangeInfo", weight=1)["symbols"]

    def funding_intervals(self) -> dict[str, int]:
        """Funding interval in hours for symbols that differ from the default 8h."""
        return {r["symbol"]: int(r["fundingIntervalHours"]) for r in self._get("/fapi/v1/fundingInfo", weight=1)}

    def klines(self, symbol: str, interval: str, *, start_ms: int | None = None, end_ms: int | None = None,
               limit: int = 500) -> list[list]:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        weight = 1 if limit < 100 else 2 if limit < 500 else 5
        return self._get("/fapi/v1/klines", params, weight)

    def long_short_ratio(self, symbol: str, end_ms: int) -> tuple[float, int] | None:
        """Global long/short account ratio of the last 5-minute snapshot at or before end_ms."""
        rows = self._get("/futures/data/globalLongShortAccountRatio",
                         {"symbol": symbol, "period": "5m", "endTime": end_ms, "limit": 1}, weight=1)
        if not rows:
            return None
        return float(rows[-1]["longShortRatio"]), int(rows[-1]["timestamp"])

    def funding_history(self, symbol: str, *, start_ms: int | None = None, end_ms: int | None = None,
                        limit: int = 100) -> list[dict]:
        params = {"symbol": symbol, "limit": limit}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        return self._get("/fapi/v1/fundingRate", params, weight=1)

    def depth(self, symbol: str, limit: int = 50) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        body = self._get("/fapi/v1/depth", {"symbol": symbol, "limit": limit}, weight=2 if limit <= 50 else 5)
        bids = [(float(p), float(q)) for p, q in body["bids"]]
        asks = [(float(p), float(q)) for p, q in body["asks"]]
        return bids, asks

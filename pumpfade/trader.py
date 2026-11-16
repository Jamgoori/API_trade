"""Signed Binance USD-M futures client for live trading (one-way position mode, USDT margin).

Conditional orders (stop-loss, take-profit) go through the Algo Order endpoints that Binance introduced in
December 2025 (POST /fapi/v1/algoOrder with algoType=CONDITIONAL); plain LIMIT / MARKET orders use /fapi/v1/order.
Order placement is never retried blindly: every order carries a client id, and callers re-query by that id.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from urllib.parse import urlencode

from hedgebot.venues.base import HttpClient, VenueError, WeightLimiter

log = logging.getLogger(__name__)

MAINNET = "https://fapi.binance.com"
TESTNET = "https://testnet.binancefuture.com"
SPOT_MAINNET = "https://api.binance.com"  # key permissions and spot balances (selftest diagnostics only)

_QUERY = re.compile(r"\?[^\s'\"]*")


def redact(text: str) -> str:
    """Drop query strings (timestamp, signature, order details) from an error message."""
    return _QUERY.sub("?…", text)


# Binance error codes handled as "already done"
NO_NEED_MARGIN_TYPE = -4046
UNKNOWN_ORDER = (-2011, -2013)


class Filters:
    def __init__(self, tick: Decimal, step: Decimal, min_qty: Decimal, min_notional: Decimal):
        self.tick, self.step, self.min_qty, self.min_notional = tick, step, min_qty, min_notional

    def __repr__(self) -> str:
        return f"tick {self.tick}, step {self.step}, min qty {self.min_qty}, min notional {self.min_notional} USDT"

    def price(self, px: float, up: bool) -> Decimal:
        q = (Decimal(str(px)) / self.tick).quantize(Decimal(1), rounding=ROUND_UP if up else ROUND_DOWN)
        return (q * self.tick).normalize()

    def qty(self, qty: float) -> Decimal:
        q = (Decimal(str(qty)) / self.step).quantize(Decimal(1), rounding=ROUND_DOWN)
        return (q * self.step).normalize()


def fmt(x: Decimal | float) -> str:
    d = x if isinstance(x, Decimal) else Decimal(str(x))
    s = format(d.normalize(), "f")
    return s


class BinanceTrader:
    def __init__(self, api_key: str, api_secret: str, base_url: str = MAINNET, *, recv_window: int = 5000,
                 spot_url: str | None = None):
        if not api_key or not api_secret:
            raise ValueError("BINANCE_API_KEY and BINANCE_API_SECRET are required for live trading")
        self.key = api_key
        self.secret = api_secret.encode()
        self.base_url = base_url
        self.recv_window = recv_window
        self.http = HttpClient(base_url, timeout=10, limiter=WeightLimiter(600, 25))
        self.http.session.headers["User-Agent"] = "pumpfade/0.1"
        spot_url = spot_url or (SPOT_MAINNET if base_url == MAINNET else None)
        self.spot_http = HttpClient(spot_url, timeout=10, limiter=WeightLimiter(600, 25)) if spot_url else None
        if self.spot_http is not None:
            self.spot_http.session.headers["User-Agent"] = "pumpfade/0.1"
        self.offset_ms = 0
        self._filters: dict[str, Filters] = {}
        self._filters_ms = 0

    # ------------------------------------------------------------------ plumbing

    def sync_time(self) -> None:
        body = HttpClient.json_or_raise(self.http.request("GET", "/fapi/v1/time"), "time")
        self.offset_ms = int(body["serverTime"]) - int(time.time() * 1000)

    def _public(self, path: str, params: dict | None = None, weight: float = 1):
        return HttpClient.json_or_raise(self.http.request("GET", path, params=params, weight=weight), path)

    def signed(self, method: str, path: str, params: dict | None = None, weight: float = 1, _retry: bool = True,
               spot: bool = False):
        http = self.spot_http if spot else self.http
        if http is None:
            raise VenueError(f"no spot API configured for {path}")
        p = {k: (fmt(v) if isinstance(v, (Decimal, float)) else v) for k, v in (params or {}).items() if v is not None}
        p["timestamp"] = int(time.time() * 1000) + self.offset_ms
        p["recvWindow"] = self.recv_window
        qs = urlencode(p)
        sig = hmac.new(self.secret, qs.encode(), hashlib.sha256).hexdigest()
        try:
            resp = http.request(method, f"{path}?{qs}&signature={sig}", weight=weight,
                                     retries=2 if method == "GET" else 0, headers={"X-MBX-APIKEY": self.key})
        except VenueError as exc:  # network errors quote the URL: re-raise without it (and without the chain)
            raise VenueError(redact(str(exc)), status=exc.status, payload=exc.payload,
                             retryable=exc.retryable) from None
        try:
            return HttpClient.json_or_raise(resp, f"{method} {path}")
        except VenueError as exc:
            code = exc.payload.get("code") if isinstance(exc.payload, dict) else None
            if code == -1021 and _retry:  # timestamp outside recvWindow: resync the clock once
                self.sync_time()
                return self.signed(method, path, params, weight, _retry=False, spot=spot)
            exc.code = code
            raise

    @staticmethod
    def code_of(exc: Exception):
        return getattr(exc, "code", None)

    # ------------------------------------------------------------------ account and symbols

    def api_restrictions(self) -> dict:
        """What this API key may do (enableFutures, enableWithdrawals, ipRestrict, ...)."""
        return self.signed("GET", "/sapi/v1/account/apiRestrictions", spot=True)

    def spot_balance(self, asset: str = "USDT") -> float:
        body = self.signed("GET", "/api/v3/account", {"omitZeroBalances": "true"}, weight=20, spot=True)
        return sum(float(b["free"]) + float(b["locked"]) for b in body.get("balances", []) if b["asset"] == asset)

    def dual_side_position(self) -> bool:
        return bool(self.signed("GET", "/fapi/v1/positionSide/dual")["dualSidePosition"])

    def account(self) -> dict:
        return self.signed("GET", "/fapi/v3/account", weight=5)

    def equity(self) -> float:
        a = self.account()
        return float(a["totalMarginBalance"])

    def positions(self) -> dict[str, float]:
        """Non-zero position amounts by symbol (negative = short)."""
        out = {}
        for p in self.account().get("positions", []):
            amt = float(p.get("positionAmt") or 0)
            if amt:
                out[p["symbol"]] = out.get(p["symbol"], 0.0) + amt
        return out

    def filters(self, symbol: str) -> Filters:
        if time.time() - self._filters_ms > 3600 or symbol not in self._filters:
            info = self._public("/fapi/v1/exchangeInfo")
            for s in info["symbols"]:
                f = {x["filterType"]: x for x in s.get("filters", [])}
                if "PRICE_FILTER" not in f or "LOT_SIZE" not in f:
                    continue
                self._filters[s["symbol"]] = Filters(
                    Decimal(f["PRICE_FILTER"]["tickSize"]), Decimal(f["LOT_SIZE"]["stepSize"]),
                    Decimal(f["LOT_SIZE"]["minQty"]), Decimal(f.get("MIN_NOTIONAL", {}).get("notional", "5")))
            self._filters_ms = time.time()
        return self._filters[symbol]

    def prepare_symbol(self, symbol: str, leverage: int = 1) -> None:
        """Isolated margin and the given leverage (only possible while the symbol has no position or orders)."""
        try:
            self.signed("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"})
        except VenueError as exc:
            if self.code_of(exc) != NO_NEED_MARGIN_TYPE:
                raise
        self.signed("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

    def book_top(self, symbol: str) -> tuple[float, float]:
        body = self._public("/fapi/v1/depth", {"symbol": symbol, "limit": 5}, weight=2)
        return float(body["bids"][0][0]), float(body["asks"][0][0])

    # ------------------------------------------------------------------ regular orders

    def new_order(self, **params) -> dict:
        params.setdefault("newOrderRespType", "RESULT")
        return self.signed("POST", "/fapi/v1/order", params)

    def query_order(self, symbol: str, cid: str) -> dict | None:
        try:
            return self.signed("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": cid})
        except VenueError as exc:
            if self.code_of(exc) in UNKNOWN_ORDER:
                return None
            raise

    def cancel_order(self, symbol: str, cid: str) -> dict | None:
        try:
            return self.signed("DELETE", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": cid})
        except VenueError as exc:
            if self.code_of(exc) in UNKNOWN_ORDER:
                return None
            raise

    def cancel_all_orders(self, symbol: str) -> None:
        self.signed("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

    # ------------------------------------------------------------------ conditional (algo) orders

    def new_algo(self, **params) -> dict:
        params["algoType"] = "CONDITIONAL"
        return self.signed("POST", "/fapi/v1/algoOrder", params)

    def query_algo(self, cid: str) -> dict | None:
        try:
            return self.signed("GET", "/fapi/v1/algoOrder", {"clientAlgoId": cid})
        except VenueError as exc:
            if self.code_of(exc) in UNKNOWN_ORDER:
                return None
            raise

    def cancel_algo(self, cid: str) -> None:
        try:
            self.signed("DELETE", "/fapi/v1/algoOrder", {"clientAlgoId": cid})
        except VenueError as exc:
            if self.code_of(exc) not in UNKNOWN_ORDER:
                raise

    def cancel_all_algos(self, symbol: str) -> None:
        self.signed("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol})

    # ------------------------------------------------------------------ history

    def user_trades(self, symbol: str, start_ms: int) -> list[dict]:
        return self.signed("GET", "/fapi/v1/userTrades", {"symbol": symbol, "startTime": start_ms, "limit": 1000},
                           weight=5)

    def funding_income(self, symbol: str, start_ms: int) -> float:
        rows = self.signed("GET", "/fapi/v1/income", {"symbol": symbol, "incomeType": "FUNDING_FEE",
                                                      "startTime": start_ms, "limit": 1000}, weight=30)
        return sum(float(r["income"]) for r in rows)

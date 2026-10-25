"""Arcus perps (Robinhood Chain) REST client.

Signing follows https://docs.arcus.xyz/api-reference/authentication :
- placeOrder / cancelOrder sign a compact, key-sorted "typed payload" built from
  engine integers (price in ticks, size in quantums) - Scheme 1.
- setLeverage / cancelAllOrders / scheduleCancel sign
  `timestamp + action + canonical_json(body)` - Scheme 2.
All signed requests carry X-API-Key (Ed25519 public key hex), X-Timestamp (unix ns)
and X-Signature (Ed25519 signature hex), plus `?address=<master wallet>`.

Order placement is asynchronous on Arcus (HTTP 202 = forwarded to the engine), so
callers poll `order_state` until the order is terminal.
"""

from __future__ import annotations

import json
import logging
import time
from decimal import Decimal

from cryptography.hazmat.primitives.asymmetric import ed25519

from ..models import BBO, AccountState, Candle, Fill, MarketInfo, OrderBook, OrderState, OrderStatus, Position, Side, Tif
from ..util import D, ONE, ZERO, exact_units, fmt_decimal, new_client_id, now_ms, quantize_to_step
from .base import HttpClient, Venue, VenueError, WeightLimiter

log = logging.getLogger(__name__)

BASE_URLS = {
    "mainnet": "https://api.arcus.xyz",
    "testnet": "https://api.testnet.arcus.xyz",
}

OP_PLACE, OP_CANCEL, OP_PLACE_UNTRIGGERED = 1, 2, 4
STOP_SLIPPAGE_BOUND = Decimal("0.05")  # market stop fills within 5% of the trigger (Arcus allows up to 10%)
SIDE_CODE = {Side.BUY: 0, Side.SELL: 1}
TIF_CODE = {Tif.GTT: 0, Tif.IOC: 2, Tif.ALO: 3}
GOOD_TIL_DAYS = 40  # Arcus requires goodTilTime >= 1 month ahead, even for IOC
INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d"}
INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
               "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "8h": 28_800_000,
               "12h": 43_200_000, "1d": 86_400_000}


def canonical_json(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


class ArcusSigner:
    """Holds the Ed25519 API signing key. The key can trade but cannot withdraw."""

    def __init__(self, private_key_hex: str, address: str, account_index: int = 0):
        key = private_key_hex.strip().removeprefix("0x")
        if len(key) != 64:
            raise ValueError("ARCUS_API_PRIVKEY must be the 32-byte Ed25519 signing key as 64 hex chars")
        self._priv = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(key))
        self.api_key = self._priv.public_key().public_bytes_raw().hex()
        if not (address.startswith("0x") and len(address) == 42):
            raise ValueError("ARCUS_ETH_ADDRESS must be a 0x-prefixed 20-byte address")
        self.address = address
        self.account_index = account_index

    def sign(self, message: bytes) -> str:
        return self._priv.sign(message).hex()

    def order_payload(
        self, *, op: int, market_id: int, ct: int, g_ns: int, p: int, q: int, reduce_only: bool,
        side: Side, tif: Tif, client_id: str | None,
    ) -> str:
        payload = {
            "ad": self.address.lower(),
            "ai": self.account_index,
            "ct": ct,
            "g": g_ns,
            "m": market_id,
            "op": op,
            "p": p,
            "q": q,
            "r": 1 if reduce_only else 0,
            "s": SIDE_CODE[side],
            "t": TIF_CODE[tif],
            "v": 1,
        }
        if client_id:
            payload["c"] = client_id
        return canonical_json(payload)

    def cancel_payload(self, *, market_id: int, ct: int, order_id: str | None = None, client_id: str | None = None) -> str:
        if (order_id is None) == (client_id is None):
            raise ValueError("cancel needs exactly one of order_id / client_id")
        payload = {"ad": self.address.lower(), "ai": self.account_index, "ct": ct, "m": market_id, "op": OP_CANCEL, "v": 1}
        if order_id is not None:
            payload["id"] = order_id
        else:
            payload["c"] = client_id
        return canonical_json(payload)

    def legacy_message(self, timestamp_ns: int, action: str, body: dict) -> bytes:
        return f"{timestamp_ns}{action}{canonical_json(body)}".encode()


_STATUS_MAP = {
    "PENDING": OrderStatus.PENDING, "ACK": OrderStatus.PENDING,
    "OPEN": OrderStatus.OPEN, "PARTIALLY_FILLED": OrderStatus.OPEN, "UNTRIGGERED": OrderStatus.OPEN,
    "TPSL_PLACED": OrderStatus.OPEN, "CANCEL_ACKNOWLEDGED": OrderStatus.OPEN, "CANCEL_PENDING": OrderStatus.OPEN,
    "FILLED": OrderStatus.FILLED, "LIQUIDATED": OrderStatus.FILLED, "ADL": OrderStatus.FILLED,
    "TPSL_TRIGGERED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELED, "MARGIN_CANCELED": OrderStatus.CANCELED, "TPSL_CANCELED": OrderStatus.CANCELED,
    "REJECTED": OrderStatus.REJECTED, "ERROR": OrderStatus.REJECTED,
}


class ArcusVenue(Venue):
    name = "arcus"

    def __init__(self, network: str = "mainnet", signer: ArcusSigner | None = None, *, timeout: float = 10.0):
        if network not in BASE_URLS:
            raise ValueError(f"unknown Arcus network {network!r}")
        self.network = network
        self.signer = signer
        self.can_trade = signer is not None
        # 1,500 weight bucket refilling at 25/s per IP; keep a margin for the operator's own UI.
        self.http = HttpClient(BASE_URLS[network], timeout=timeout, limiter=WeightLimiter(1200, 20))
        self._markets: dict[str, MarketInfo] = {}
        self._by_id: dict[int, str] = {}
        self._clock_offset_ns = 0
        self._dms_deadline_us = 0

    # ------------------------------------------------------------------ helpers
    def _get(self, path: str, weight: float, params: dict | None = None):
        resp = self.http.request("GET", path, weight=weight, params=params)
        return self.http.json_or_raise(resp, f"GET {path}")

    def _ts_ns(self) -> int:
        return time.time_ns() + self._clock_offset_ns

    def sync_clock(self) -> None:
        """X-Timestamp must be within +-30s of the gateway clock."""
        before = time.time_ns()
        body = self._get("/v1/time", 1)
        after = time.time_ns()
        self._clock_offset_ns = int(body["timeNs"]) - (before + after) // 2
        if abs(self._clock_offset_ns) > 2_000_000_000:
            log.warning("local clock is %.1fs off the Arcus gateway; compensating", self._clock_offset_ns / 1e9)

    def _require_signer(self) -> ArcusSigner:
        if not self.signer:
            raise VenueError("arcus: trading needs ARCUS_API_PRIVKEY / ARCUS_ETH_ADDRESS")
        return self.signer

    def _signed_post(self, path: str, body_text: str, ts: int, signature: str, context: str):
        signer = self._require_signer()
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": signer.api_key,
            "X-Timestamp": str(ts),
            "X-Signature": signature,
        }
        resp = self.http.request(
            "POST", path, weight=0, retries=0, params={"address": signer.address}, data=body_text, headers=headers
        )
        if resp.status_code == 401:
            # Clock drift (X-Timestamp outside +-30s) or an expired / revoked API key.
            try:
                self.sync_clock()
            except VenueError:
                pass
            raise VenueError(f"{context}: HTTP 401 {resp.text[:200]} (API key expired or revoked, or clock off)",
                             status=401, retryable=True)
        return self.http.json_or_raise(resp, context)

    def _legacy_post(self, path: str, action: str, body: dict, weight: float = 0):
        signer = self._require_signer()
        if weight and self.http.limiter:
            self.http.limiter.acquire(weight)
        ts = self._ts_ns()
        sig = signer.sign(signer.legacy_message(ts, action, body))
        return self._signed_post(path, canonical_json(body), ts, sig, f"POST {path}")

    def market_id(self, symbol: str) -> int:
        return int(self.market(symbol).extra["marketId"])

    # -------------------------------------------------------------- market data
    def refresh_markets(self) -> dict[str, MarketInfo]:
        body = self._get("/v1/markets", 20)
        markets: dict[str, MarketInfo] = {}
        for m in body["markets"]:
            imf = D(m["initialMarginFraction"])
            off_imf = D(m.get("offHoursInitialMarginFraction") or m["initialMarginFraction"])
            info = MarketInfo(
                venue=self.name,
                symbol=m["marketDisplayName"],
                step_size=D(m["stepSize"]),
                min_notional=D(m.get("minOrderNotional") or 5),
                max_leverage=(ONE / imf).quantize(Decimal("0.01")) if imf > 0 else ONE,
                off_hours_max_leverage=(ONE / off_imf).quantize(Decimal("0.01")) if off_imf > 0 else ONE,
                maintenance_margin_fraction=D(m["maintenanceMarginFraction"]),
                maker_fee=D(m.get("effectiveMakerFeePpm", m.get("baseMakerFeePpm", 0))) / 1_000_000,
                taker_fee=D(m.get("effectiveTakerFeePpm", m.get("baseTakerFeePpm", 0))) / 1_000_000,
                is_rwa=m.get("category") != "CRYPTO",
                outside_rth=bool(m.get("isOutsideRth")),
                isolated_only=False,
                online=m.get("status") == "ONLINE",
                funding_rate_1h=D(m.get("fundingRate") or 0),
                mark_price=D(m.get("markPrice") or 0),
                oracle_price=D(m.get("oraclePrice") or 0),
                volume_24h_usd=D(m.get("volume24hNotional") or 0),
                open_interest_usd=D(m.get("openInterest") or 0) * D(m.get("markPrice") or 0),
                min_size=D(m.get("minOrderSize") or 0),
                max_order_size=D(m["maxOrderSize"]) if m.get("maxOrderSize") else None,
                extra={
                    "marketId": m["marketId"],
                    "tickSize": m["tickSize"],
                    "tickTiers": m.get("tickTiers") or [],
                    "category": m.get("category"),
                    "upperTradingBound": m.get("upperTradingBound"),
                    "lowerTradingBound": m.get("lowerTradingBound"),
                    "currentSettlementPrice": m.get("currentSettlementPrice"),
                    "liquidationFeePpm": m.get("liquidationFeePpm"),
                    "openInterestCapNotional": m.get("openInterestCapNotional"),
                },
            )
            markets[info.symbol] = info
            self._by_id[int(m["marketId"])] = info.symbol
        self._markets = markets
        return markets

    def market(self, symbol: str) -> MarketInfo:
        if symbol not in self._markets:
            self.refresh_markets()
        try:
            return self._markets[symbol]
        except KeyError:
            raise VenueError(f"arcus: unknown market {symbol}") from None

    def bbo(self, symbol: str) -> BBO:
        body = self._get(f"/v1/bbo/{symbol}", 2)
        bid, ask = body.get("bestBid"), body.get("bestAsk")
        if not bid or not ask:
            raise VenueError(f"arcus: empty book side for {symbol}", retryable=True)
        return BBO(D(bid["price"]), D(bid["size"]), D(ask["price"]), D(ask["size"]), time.time())

    def book(self, symbol: str, depth: int = 20) -> OrderBook:
        depth = max(1, min(depth, 100))
        body = self._get(f"/v1/l2OrderBook/{symbol}", 2 + depth // 20, {"nLevels": depth})
        bids = [(D(p), D(s)) for p, s in body.get("bids", [])]
        asks = [(D(p), D(s)) for p, s in body.get("asks", [])]
        return OrderBook(bids, asks, time.time())

    def _tick_for(self, info: MarketInfo, price: Decimal) -> Decimal:
        tick = D(info.extra["tickSize"])
        for tier in info.extra.get("tickTiers") or []:
            upto = tier.get("upToPrice")
            if upto is None or price <= D(upto):
                return D(tier["tick"])
        return tick

    def round_price(self, symbol: str, price: Decimal, mode: str = "nearest") -> Decimal:
        info = self.market(symbol)
        rounded = quantize_to_step(price, self._tick_for(info, price), mode)
        # Crossing a tier boundary can change the tick; re-round once with the new band.
        return quantize_to_step(rounded, self._tick_for(info, rounded), mode)

    def round_size(self, symbol: str, size: Decimal) -> Decimal:
        return quantize_to_step(abs(size), self.market(symbol).step_size, "down")

    def tick_size(self, symbol: str, price: Decimal) -> Decimal:
        return self._tick_for(self.market(symbol), D(price))

    def funding_history(self, symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, Decimal]]:
        out: dict[int, Decimal] = {}
        to_us = end_ms * 1000
        while True:
            body = self._get("/v1/fundingRates", 20 + 50,
                             {"market": symbol, "from": start_ms * 1000, "to": to_us, "limit": 1000})
            rows = body.get("fundingRates", [])
            for r in rows:
                out[int(r["time"]) // 1000] = D(r["fundingRate"])
            if len(rows) < 1000:
                break
            oldest = min(int(r["time"]) for r in rows)
            if oldest <= start_ms * 1000:
                break
            to_us = oldest - 1
        return sorted((t, v) for t, v in out.items() if start_ms <= t <= end_ms)

    def candles(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[Candle]:
        if interval not in INTERVALS:
            raise ValueError(f"arcus: unsupported interval {interval}")
        bars: dict[int, Candle] = {}
        to_us = end_ms * 1000
        step_ms = INTERVAL_MS[interval]
        while to_us // 1000 > start_ms:
            body = self._get("/v1/candles", 45, {"market": symbol, "timeframe": interval, "to": to_us, "countback": 1500})
            rows = body.get("candles", [])
            if not rows:
                break
            for r in rows:
                t = int(r["openTime"]) // 1000
                bars[t] = Candle(t, D(r["open"]), D(r["high"]), D(r["low"]), D(r["close"]), D(r.get("notionalVolume") or 0))
            oldest = min(int(r["openTime"]) for r in rows) // 1000
            if oldest - step_ms < start_ms or len(rows) < 1500:
                break
            to_us = oldest * 1000
        return [bars[t] for t in sorted(bars) if start_ms <= t <= end_ms]

    # ------------------------------------------------------------------ account
    def account(self) -> AccountState:
        signer = self._require_signer()
        params = {"address": signer.address, "accountIndex": signer.account_index}
        resp = self.http.request("GET", "/v1/account", weight=2, params=params)
        if resp.status_code == 404:
            return AccountState(self.name, ZERO, ZERO, ZERO, {}, time.time())
        body = self.http.json_or_raise(resp, "GET /v1/account")
        positions: dict[str, Position] = {}
        maintenance = ZERO
        for mid, p in (body.get("positions") or {}).items():
            symbol = p.get("marketDisplayName") or self._by_id.get(int(mid)) or str(mid)
            size = D(p.get("size") or 0)
            if size == 0:
                continue
            mark = D(p.get("markPx") or 0)
            info = self._markets.get(symbol)
            if mark == 0 and info:
                mark = info.mark_price
            entry = D(p.get("averageEntryPrice") or 0)
            mmf = info.maintenance_margin_fraction if info else Decimal("0.05")
            mode = "isolated" if (p.get("marginMode") == "ISOLATED") else "cross"
            margin_used = D(p.get("marginUsed") or 0)
            liq = isolated_liquidation_price(size, entry, margin_used, mmf) if mode == "isolated" else None
            positions[symbol] = Position(symbol, size, entry, mark, liq, mode, margin_used, size * (mark - entry))
            maintenance += abs(size) * mark * mmf
        return AccountState(
            venue=self.name,
            equity=D(body.get("equity") or 0),
            free_collateral=D(body.get("freeCollateral") or 0),
            maintenance_margin=maintenance,
            positions=positions,
            ts=time.time(),
        )

    # ------------------------------------------------------------------ trading
    def place_order(self, symbol, side, size, price, tif, *, reduce_only=False, client_id=None) -> OrderState:
        signer = self._require_signer()
        info = self.market(symbol)
        size = D(size)
        price = D(price)
        client_id = client_id or new_client_id()
        p = exact_units(price, D(info.extra["tickSize"]))
        q = exact_units(size, info.step_size)
        ts = self._ts_ns()
        good_til_us = ts // 1000 + GOOD_TIL_DAYS * 86_400 * 1_000_000
        payload = signer.order_payload(
            op=OP_PLACE, market_id=int(info.extra["marketId"]), ct=ts, g_ns=good_til_us * 1000,
            p=p, q=q, reduce_only=reduce_only, side=side, tif=tif, client_id=client_id,
        )
        body = {
            "address": signer.address,
            "accountIndex": signer.account_index,
            "marketId": int(info.extra["marketId"]),
            "orderSide": side.value,
            "orderType": "LIMIT",
            "quantity": fmt_decimal(size),
            "price": fmt_decimal(price),
            "timeInForce": tif.value,
            "goodTilTime": str(good_til_us),
            "timestamp": ts,
            "reduceOnly": reduce_only,
            "clientId": client_id,
        }
        try:
            resp = self._signed_post("/v1/placeOrder", canonical_json(body), ts, signer.sign(payload.encode()),
                                     f"placeOrder {symbol}")
        except VenueError as exc:
            # A 4xx here is a definitive rejection (tick, oracle deviation, size...).
            if exc.status and 400 <= exc.status < 500 and exc.status not in (401, 429):
                return OrderState("", symbol, side, price, size, tif, OrderStatus.REJECTED, client_id=client_id,
                                  reduce_only=reduce_only, reject_reason=str(exc.payload), created_ts=time.time())
            if exc.status not in (401, 429, 502):  # 502 = not submitted; otherwise it may be live
                self._cancel_quietly(symbol, client_id)
            raise
        state = OrderState(
            order_id=str(resp.get("orderId", "")),
            symbol=symbol, side=side, price=price, size=size, tif=tif,
            status=_STATUS_MAP.get(str(resp.get("status")), OrderStatus.PENDING),
            client_id=client_id, reduce_only=reduce_only, created_ts=time.time(),
        )
        if resp.get("filledSize"):
            state.filled = D(resp["filledSize"])
        if resp.get("rejectionReason"):
            state.reject_reason = str(resp["rejectionReason"])
        return state

    def order_state(self, order: OrderState) -> OrderState:
        signer = self._require_signer()
        if not order.order_id:
            return order
        params = {"address": signer.address, "accountIndex": signer.account_index}
        resp = self.http.request("GET", f"/v1/order/{order.order_id}", weight=2, params=params)
        if resp.status_code == 404:
            # Placement is asynchronous; give the store a moment before calling it lost.
            if time.time() - order.created_ts > 20:
                if order.client_id:  # make sure a lagging order store is not hiding a live order
                    self._cancel_quietly(order.symbol, order.client_id)
                order.status = OrderStatus.REJECTED
                order.reject_reason = order.reject_reason or "order not found"
            return order
        body = self.http.json_or_raise(resp, f"GET /v1/order/{order.order_id}")
        raw = str(body.get("status") or body.get("state") or "")
        order.status = _STATUS_MAP.get(raw, order.status)
        if body.get("filledSize") is not None:
            order.filled = D(body["filledSize"])
        if body.get("avgFillPrice"):
            order.avg_price = D(body["avgFillPrice"])
        if body.get("rejectionReason"):
            order.reject_reason = str(body["rejectionReason"])
        if order.status is OrderStatus.FILLED and order.filled == 0:
            order.filled = order.size
        return order

    def cancel_order(self, symbol: str, order_id: str) -> None:
        self._cancel(symbol, order_id=order_id)

    def _cancel(self, symbol: str, *, order_id: str | None = None, client_id: str | None = None) -> None:
        signer = self._require_signer()
        market_id = self.market_id(symbol)
        ts = self._ts_ns()
        payload = signer.cancel_payload(market_id=market_id, ct=ts, order_id=order_id, client_id=client_id)
        body = {
            "address": signer.address,
            "accountIndex": signer.account_index,
            "marketId": market_id,
            "timestamp": ts,
        }
        body.update({"kind": "orderId", "orderId": order_id} if order_id else {"kind": "clientId", "clientId": client_id})
        try:
            self._signed_post("/v1/cancelOrder", canonical_json(body), ts, signer.sign(payload.encode()),
                              f"cancelOrder {symbol}")
        except VenueError as exc:
            # Already filled / already canceled is not an error for our purposes.
            if exc.status in (400, 404):
                log.info("arcus cancel %s: %s", order_id or client_id, exc)
                return
            raise

    def _cancel_quietly(self, symbol: str, client_id: str) -> None:
        """Best effort: cancel by clientId an order whose placement outcome is unknown."""
        try:
            self._cancel(symbol, client_id=client_id)
            log.warning("arcus: placement of %s %s was ambiguous; sent cancel by clientId", symbol, client_id)
        except VenueError as exc:
            log.warning("arcus: cancel by clientId %s failed: %s", client_id, exc)

    def cancel_all(self) -> None:
        signer = self._require_signer()
        self._legacy_post("/v1/cancelAllOrders", "cancelAllOrders",
                          {"address": signer.address, "accountIndex": signer.account_index})

    def cancel_resting(self) -> None:
        """Cancel resting orders (status OPEN), keeping untriggered position stops."""
        signer = self._require_signer()
        body = self._get("/v1/openOrders", 2, {"address": signer.address, "accountIndex": signer.account_index,
                                               "status": "OPEN"})
        for o in body.get("orders", []):
            if o.get("status") == "OPEN" and not o.get("tpslType"):
                self.cancel_order(o["marketDisplayName"], o["orderId"])

    def set_leverage(self, symbol: str, leverage: int, cross: bool) -> None:
        signer = self._require_signer()
        body = {
            "address": signer.address,
            "accountIndex": signer.account_index,
            "marketId": self.market_id(symbol),
            "leverage": int(leverage),
            "isolated": not cross,
        }
        resp = self._legacy_post("/v1/setLeverage", "setLeverage", body, weight=125)
        if isinstance(resp, dict) and resp.get("status") == "REJECTED":
            raise VenueError(f"arcus setLeverage {symbol}: {resp.get('rejectReason')}", payload=resp)

    def place_stop(self, symbol: str, side: Side, size: Decimal, trigger: Decimal) -> OrderState:
        """Position-level stop-loss (positionTpsl): closes the whole position at trigger time."""
        signer = self._require_signer()
        info = self.market(symbol)
        trigger = self.round_price(symbol, D(trigger), "nearest")
        bound = trigger * (1 - STOP_SLIPPAGE_BOUND) if side is Side.SELL else trigger * (1 + STOP_SLIPPAGE_BOUND)
        price = self.round_price(symbol, bound, "down" if side is Side.SELL else "up")
        client_id = new_client_id("bs")
        ts = self._ts_ns()
        good_til_us = ts // 1000 + GOOD_TIL_DAYS * 86_400 * 1_000_000
        payload = signer.order_payload(
            op=OP_PLACE_UNTRIGGERED, market_id=int(info.extra["marketId"]), ct=ts, g_ns=good_til_us * 1000,
            p=exact_units(price, D(info.extra["tickSize"])), q=0, reduce_only=True, side=side, tif=Tif.IOC,
            client_id=client_id,
        )
        signature = signer.sign(payload.encode())
        element = {
            "address": signer.address,
            "accountIndex": signer.account_index,
            "marketId": int(info.extra["marketId"]),
            "orderSide": side.value,
            "orderType": "MARKET",
            "quantity": "0",
            "price": fmt_decimal(price),
            "timeInForce": "IOC",
            "goodTilTime": str(good_til_us),
            "timestamp": ts,
            "reduceOnly": True,
            "stopPrice": fmt_decimal(trigger),
            "tpslType": "STOP_LOSS",
            "isPositionTPSL": True,
            "clientId": client_id,
            "signature": signature,
        }
        state = OrderState("", symbol, side, price, D(size), Tif.IOC, OrderStatus.PENDING, client_id=client_id,
                           reduce_only=True, created_ts=time.time(), trigger_price=trigger)
        try:
            resp = self._signed_post("/v1/batchPlaceOrders", canonical_json({"grouping": "positionTpsl",
                                                                              "orders": [element]}),
                                     ts, signature, f"positionTpsl {symbol}")
        except VenueError as exc:
            if exc.status and 400 <= exc.status < 500 and exc.status not in (401, 429):
                state.status, state.reject_reason = OrderStatus.REJECTED, str(exc.payload)
                return state
            if exc.status not in (401, 429, 502):  # may have been accepted: do not leave an untracked stop
                self._cancel_quietly(symbol, client_id)
            raise
        row = (resp.get("responses") or [{}])[0]
        state.order_id = str(row.get("orderId", ""))
        if row.get("status") in ("ERROR", "REJECTED") or row.get("error"):
            state.status = OrderStatus.REJECTED
            state.reject_reason = str(row.get("error") or row.get("rejectionReason"))
        else:
            # 202 rows say ACK: the engine can still reject it (e.g. POSITION_TPSL_ALREADY_EXISTS);
            # the backstop manager polls it with order_state.
            state.status = _STATUS_MAP.get(str(row.get("status")), OrderStatus.PENDING)
        return state

    def heartbeat(self, lead_s: int = 60) -> None:
        """Arm/refresh the dead man's switch so resting maker orders die with the bot."""
        signer = self._require_signer()
        deadline = self._ts_ns() // 1000 + int(lead_s * 1_000_000)
        self._legacy_post("/v1/scheduleCancel", "scheduleCancel",
                          {"address": signer.address, "accountIndex": signer.account_index, "time": deadline})
        self._dms_deadline_us = deadline

    def disarm_heartbeat(self) -> None:
        signer = self._require_signer()
        self._legacy_post("/v1/scheduleCancel", "scheduleCancel",
                          {"address": signer.address, "accountIndex": signer.account_index})

    def recent_fills(self, since_ms: int) -> list[Fill]:
        signer = self._require_signer()
        body = self._get("/v1/fills", 20 + 50, {"address": signer.address, "accountIndex": signer.account_index,
                                                "from": since_ms * 1000})
        fills = []
        for f in body.get("fills", []):
            fills.append(Fill(
                venue=self.name,
                symbol=f.get("marketDisplayName") or self._by_id.get(int(f.get("marketId", 0)), ""),
                order_id=str(f.get("orderId", "")),
                side=Side(f["side"]),
                size=D(f["size"]),
                price=D(f["price"]),
                fee=D(f.get("fee") or 0),
                liquidity="maker" if f.get("role") == "MAKER" else "taker",
                ts_ms=int(f.get("createdAt", now_ms() * 1000)) // 1000,
                fill_id=str(f.get("tradeId", "")),
            ))
        return fills


def isolated_liquidation_price(size: Decimal, entry: Decimal, margin: Decimal, mmf: Decimal) -> Decimal | None:
    """Price where isolated margin + uPnL equals maintenance margin.

    long  (s>0): M + s(P-E) = s*P*m        -> P = (sE - M) / (s(1-m))
    short (s<0): M + s(P-E) = -s*P*m       -> P = (sE - M) / (s(1+m))
    """
    if size == 0 or margin <= 0:
        return None
    denom = size * (ONE - mmf) if size > 0 else size * (ONE + mmf)
    if denom == 0:
        return None
    price = (size * entry - margin) / denom
    return price if price > 0 else ZERO

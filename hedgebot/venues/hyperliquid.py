"""Hyperliquid HIP-3 dexes (trade.xyz = `xyz`, Entropy = `io`).

Each HIP-3 dex keeps its own margin account, so every dex is its own Venue
(`hl:xyz`, `hl:io`). They share one wallet and one signing client.

Market data goes straight to the /info endpoint. Trading uses the official
hyperliquid-python-sdk with an API (agent) wallet: it signs orders for the main
account but cannot withdraw. Info reads must use the main account address.
"""

from __future__ import annotations

import logging
import math
import time
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

from ..models import BBO, AccountState, Candle, Fill, MarketInfo, OrderBook, OrderState, OrderStatus, Position, Side, Tif
from ..util import D, ONE, ZERO, quantize_to_step
from .base import HttpClient, Venue, VenueError, WeightLimiter

log = logging.getLogger(__name__)

BASE_URLS = {
    "mainnet": "https://api.hyperliquid.xyz",
    "testnet": "https://api.hyperliquid-testnet.xyz",
}
PERP_MAX_DECIMALS = 6
MIN_ORDER_NOTIONAL = Decimal(10)
GROWTH_MODE_FEE_SCALE = Decimal("0.1")
TIF_WIRE = {Tif.ALO: "Alo", Tif.IOC: "Ioc", Tif.GTT: "Gtc"}
HL_INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d"}
INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
               "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "8h": 28_800_000,
               "12h": 43_200_000, "1d": 86_400_000}


def hl_round_price(price: Decimal, sz_decimals: int, mode: str = "nearest") -> Decimal:
    """Hyperliquid perp price rule: <= 5 significant figures and <= 6 - szDecimals decimals.

    Integer prices are always valid regardless of significant figures.
    """
    rounding = {"down": ROUND_FLOOR, "up": ROUND_CEILING, "nearest": ROUND_HALF_EVEN}[mode]
    return price.quantize(hl_tick_size(price, sz_decimals), rounding=rounding)


def hl_tick_size(price: Decimal, sz_decimals: int) -> Decimal:
    if price <= 0:
        raise ValueError("price must be positive")
    exponent = math.floor(math.log10(float(price)))
    decimals = max(0, min(PERP_MAX_DECIMALS - sz_decimals, 4 - exponent))
    return Decimal(1).scaleb(-decimals)


class HyperliquidClient:
    """Shared /info reader + SDK exchange handle for every HIP-3 dex the bot trades."""

    def __init__(self, network: str = "mainnet", account_address: str | None = None,
                 agent_private_key: str | None = None, dexes: list[str] | None = None, timeout: float = 10.0):
        if network not in BASE_URLS:
            raise ValueError(f"unknown Hyperliquid network {network!r}")
        self.network = network
        self.base_url = BASE_URLS[network]
        self.account_address = account_address
        self.agent_private_key = agent_private_key
        self.dexes = dexes or []
        # 1,200 weight/min per IP; stay below it.
        self.http = HttpClient(self.base_url, timeout=timeout, limiter=WeightLimiter(900, 15))
        self._exchange = None

    @property
    def can_trade(self) -> bool:
        return bool(self.account_address and self.agent_private_key)

    def info(self, body: dict, weight: float = 20):
        resp = self.http.request("POST", "/info", weight=weight, json=body)
        return self.http.json_or_raise(resp, f"info {body.get('type')}")

    @property
    def exchange(self):
        if self._exchange is None:
            if not self.can_trade:
                raise VenueError("hyperliquid: trading needs HL_ACCOUNT_ADDRESS / HL_AGENT_PRIVKEY")
            from eth_account import Account
            from hyperliquid.exchange import Exchange

            wallet = Account.from_key(self.agent_private_key)
            self._exchange = Exchange(
                wallet, self.base_url, account_address=self.account_address, perp_dexs=["", *self.dexes]
            )
        return self._exchange


class HyperliquidVenue(Venue):
    def __init__(self, client: HyperliquidClient, dex: str, *, base_maker_fee: Decimal = Decimal("0.00015"),
                 base_taker_fee: Decimal = Decimal("0.00045")):
        if not dex:
            raise ValueError("HyperliquidVenue is for HIP-3 dexes; pass e.g. 'xyz' or 'io'")
        self.client = client
        self.dex = dex
        self.name = f"hl:{dex}"
        self.can_trade = client.can_trade
        self.base_maker_fee = D(base_maker_fee)
        self.base_taker_fee = D(base_taker_fee)
        self._markets: dict[str, MarketInfo] = {}

    # -------------------------------------------------------------- market data
    def refresh_markets(self) -> dict[str, MarketInfo]:
        body = self.client.info({"type": "metaAndAssetCtxs", "dex": self.dex}, 20)
        if not body or not isinstance(body, list) or body[0] is None:
            raise VenueError(f"{self.name}: dex not found on {self.client.network}")
        meta, ctxs = body
        markets: dict[str, MarketInfo] = {}
        for u, ctx in zip(meta["universe"], ctxs):
            max_lev = D(u.get("maxLeverage") or 1)
            growth = u.get("growthMode") == "enabled"
            fee_mult = (ONE + D(u.get("deployerFeeScale") or 1)) * (GROWTH_MODE_FEE_SCALE if growth else ONE)
            mark = D(ctx.get("markPx") or ctx.get("midPx") or 0)
            info = MarketInfo(
                venue=self.name,
                symbol=u["name"],
                step_size=Decimal(1).scaleb(-int(u["szDecimals"])),
                min_notional=MIN_ORDER_NOTIONAL,
                max_leverage=max_lev,
                off_hours_max_leverage=max_lev,
                maintenance_margin_fraction=ONE / (2 * max_lev),
                maker_fee=self.base_maker_fee * fee_mult,
                taker_fee=self.base_taker_fee * fee_mult,
                is_rwa=True,
                outside_rth=False,
                isolated_only=bool(u.get("onlyIsolated")) or u.get("marginMode") in ("noCross", "strictIsolated"),
                online=not u.get("isDelisted", False),
                funding_rate_1h=D(ctx.get("funding") or 0),
                mark_price=mark,
                oracle_price=D(ctx.get("oraclePx") or 0),
                volume_24h_usd=D(ctx.get("dayNtlVlm") or 0),
                open_interest_usd=D(ctx.get("openInterest") or 0) * mark,
                min_size=Decimal(1).scaleb(-int(u["szDecimals"])),
                extra={
                    "szDecimals": int(u["szDecimals"]),
                    "growthMode": growth,
                    "deployerFeeScale": u.get("deployerFeeScale"),
                    "marginMode": u.get("marginMode"),
                    "midPx": ctx.get("midPx"),
                },
            )
            markets[info.symbol] = info
        self._markets = markets
        return markets

    def market(self, symbol: str) -> MarketInfo:
        if symbol not in self._markets:
            self.refresh_markets()
        try:
            return self._markets[symbol]
        except KeyError:
            raise VenueError(f"{self.name}: unknown market {symbol}") from None

    def book(self, symbol: str, depth: int = 20) -> OrderBook:
        body = self.client.info({"type": "l2Book", "coin": symbol}, 2)
        levels = body.get("levels") or [[], []]
        bids = [(D(x["px"]), D(x["sz"])) for x in levels[0][:depth]]
        asks = [(D(x["px"]), D(x["sz"])) for x in levels[1][:depth]]
        return OrderBook(bids, asks, time.time())

    def bbo(self, symbol: str) -> BBO:
        book = self.book(symbol, 1)
        if not book.bids or not book.asks:
            raise VenueError(f"{self.name}: empty book side for {symbol}", retryable=True)
        return book.bbo()

    def round_price(self, symbol: str, price: Decimal, mode: str = "nearest") -> Decimal:
        return hl_round_price(D(price), int(self.market(symbol).extra["szDecimals"]), mode)

    def round_size(self, symbol: str, size: Decimal) -> Decimal:
        return quantize_to_step(abs(D(size)), self.market(symbol).step_size, "down")

    def tick_size(self, symbol: str, price: Decimal) -> Decimal:
        return hl_tick_size(D(price), int(self.market(symbol).extra["szDecimals"]))

    def funding_history(self, symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, Decimal]]:
        out: dict[int, Decimal] = {}
        cursor = start_ms
        while cursor < end_ms:
            rows = self.client.info({"type": "fundingHistory", "coin": symbol, "startTime": cursor, "endTime": end_ms}, 20 + 25)
            if not rows:
                break
            for r in rows:
                out[int(r["time"])] = D(r["fundingRate"])
            last = max(int(r["time"]) for r in rows)
            if len(rows) < 500 or last <= cursor:
                break
            cursor = last + 1
        return sorted(out.items())

    def candles(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[Candle]:
        if interval not in HL_INTERVALS:
            raise ValueError(f"hyperliquid: unsupported interval {interval}")
        bars: dict[int, Candle] = {}
        cursor = start_ms
        while cursor < end_ms:
            rows = self.client.info({"type": "candleSnapshot", "req": {
                "coin": symbol, "interval": interval, "startTime": cursor, "endTime": end_ms}}, 20 + 60)
            if not rows:
                break
            for r in rows:
                t = int(r["t"])
                close = D(r["c"])
                bars[t] = Candle(t, D(r["o"]), D(r["h"]), D(r["l"]), close, D(r["v"]) * close)
            last = max(int(r["t"]) for r in rows)
            if last <= cursor or len(rows) < 5000:
                break
            cursor = last + INTERVAL_MS[interval]
        return [bars[t] for t in sorted(bars) if start_ms <= t <= end_ms]

    # ------------------------------------------------------------------ account
    def _user(self) -> str:
        if not self.client.account_address:
            raise VenueError("hyperliquid: HL_ACCOUNT_ADDRESS is not set")
        return self.client.account_address

    def account(self) -> AccountState:
        body = self.client.info({"type": "clearinghouseState", "user": self._user(), "dex": self.dex}, 2)
        summary = body.get("marginSummary") or {}
        positions: dict[str, Position] = {}
        for ap in body.get("assetPositions", []):
            p = ap.get("position") or {}
            size = D(p.get("szi") or 0)
            if size == 0:
                continue
            symbol = p["coin"]
            value = D(p.get("positionValue") or 0)
            mark = value / abs(size) if size else ZERO
            lev = p.get("leverage") or {}
            liq = p.get("liquidationPx")
            positions[symbol] = Position(
                symbol=symbol,
                size=size,
                entry_price=D(p.get("entryPx") or 0),
                mark_price=mark,
                liquidation_price=D(liq) if liq else None,
                margin_mode="isolated" if lev.get("type") == "isolated" else "cross",
                margin_used=D(p.get("marginUsed") or 0),
                unrealized_pnl=D(p.get("unrealizedPnl") or 0),
            )
        return AccountState(
            venue=self.name,
            equity=D(summary.get("accountValue") or 0),
            free_collateral=D(body.get("withdrawable") or 0),
            maintenance_margin=D(body.get("crossMaintenanceMarginUsed") or 0),
            positions=positions,
            ts=time.time(),
        )

    # ------------------------------------------------------------------ trading
    def place_order(self, symbol, side, size, price, tif, *, reduce_only=False, client_id=None) -> OrderState:
        size, price = D(size), D(price)
        state = OrderState("", symbol, side, price, size, tif, OrderStatus.PENDING,
                           client_id=client_id, reduce_only=reduce_only, created_ts=time.time())
        try:
            resp = self.client.exchange.order(
                symbol, side is Side.BUY, float(size), float(price), {"limit": {"tif": TIF_WIRE[tif]}},
                reduce_only=reduce_only,
            )
        except Exception as exc:  # the SDK raises plain exceptions on transport errors
            raise VenueError(f"{self.name} order {symbol}: {exc}", retryable=True) from exc
        if resp.get("status") != "ok":
            state.status = OrderStatus.REJECTED
            state.reject_reason = str(resp.get("response"))
            return state
        status = resp["response"]["data"]["statuses"][0]
        if "filled" in status:
            f = status["filled"]
            state.order_id = str(f.get("oid", ""))
            state.filled = D(f["totalSz"])
            state.avg_price = D(f["avgPx"])
            # An IOC that filled partially is done; the rest was canceled.
            state.status = OrderStatus.FILLED if state.filled >= size or tif is Tif.IOC else OrderStatus.OPEN
        elif "resting" in status:
            state.order_id = str(status["resting"]["oid"])
            state.status = OrderStatus.OPEN
        else:
            error = str(status.get("error", status))
            # IOC with nothing to match comes back as an error; that is a clean zero fill.
            state.status = OrderStatus.CANCELED if "could not immediately match" in error.lower() else OrderStatus.REJECTED
            state.reject_reason = error
        return state

    def order_state(self, order: OrderState) -> OrderState:
        if not order.order_id or order.status.done:
            return order
        body = self.client.info({"type": "orderStatus", "user": self._user(), "oid": int(order.order_id)}, 2)
        if body.get("status") != "order":
            return order
        info = body["order"]
        o = info.get("order") or {}
        orig, remaining = D(o.get("origSz") or order.size), D(o.get("sz") or 0)
        order.filled = max(ZERO, orig - remaining)
        raw = str(info.get("status", ""))
        if raw == "open":
            order.status = OrderStatus.OPEN
        elif raw == "filled":
            order.status, order.filled = OrderStatus.FILLED, orig
        elif raw.endswith("anceled") or raw in ("triggered",):
            order.status = OrderStatus.CANCELED
        elif raw == "rejected":
            order.status = OrderStatus.REJECTED
        return order

    def cancel_order(self, symbol: str, order_id: str) -> None:
        try:
            self.client.exchange.cancel(symbol, int(order_id))
        except Exception as exc:
            raise VenueError(f"{self.name} cancel {order_id}: {exc}", retryable=True) from exc

    def cancel_all(self) -> None:
        # frontendOpenOrders also lists untriggered stop orders
        rows = self.client.info({"type": "frontendOpenOrders", "user": self._user(), "dex": self.dex}, 20)
        for o in rows:
            self.cancel_order(o["coin"], str(o["oid"]))

    def place_stop(self, symbol: str, side: Side, size: Decimal, trigger: Decimal) -> OrderState:
        """Reduce-only stop-market trigger order (sized; refreshed by the backstop manager)."""
        trigger = self.round_price(symbol, D(trigger), "nearest")
        bound = trigger * Decimal("0.9") if side is Side.SELL else trigger * Decimal("1.1")
        limit = self.round_price(symbol, bound, "down" if side is Side.SELL else "up")
        size = self.round_size(symbol, size)
        state = OrderState("", symbol, side, limit, size, Tif.GTT, OrderStatus.PENDING, reduce_only=True,
                           created_ts=time.time(), trigger_price=trigger)
        try:
            resp = self.client.exchange.order(
                symbol, side is Side.BUY, float(size), float(limit),
                {"trigger": {"triggerPx": float(trigger), "isMarket": True, "tpsl": "sl"}}, reduce_only=True,
            )
        except Exception as exc:
            raise VenueError(f"{self.name} stop {symbol}: {exc}", retryable=True) from exc
        status = resp["response"]["data"]["statuses"][0] if resp.get("status") == "ok" else {"error": resp}
        if "resting" in status:
            state.order_id, state.status = str(status["resting"]["oid"]), OrderStatus.OPEN
        else:
            state.status, state.reject_reason = OrderStatus.REJECTED, str(status.get("error", status))
        return state

    def set_leverage(self, symbol: str, leverage: int, cross: bool) -> None:
        info = self.market(symbol)
        if info.isolated_only:
            cross = False
        resp = self.client.exchange.update_leverage(int(leverage), symbol, is_cross=cross)
        if resp.get("status") != "ok":
            raise VenueError(f"{self.name} updateLeverage {symbol}: {resp}", payload=resp)

    def recent_fills(self, since_ms: int) -> list[Fill]:
        rows = self.client.info({"type": "userFillsByTime", "user": self._user(), "startTime": since_ms,
                                 "aggregateByTime": False}, 20 + 40)
        prefix = f"{self.dex}:"
        fills = []
        for r in rows:
            if not str(r.get("coin", "")).startswith(prefix):
                continue
            fills.append(Fill(
                venue=self.name,
                symbol=r["coin"],
                order_id=str(r.get("oid", "")),
                side=Side.BUY if r.get("side") == "B" else Side.SELL,
                size=D(r["sz"]),
                price=D(r["px"]),
                fee=D(r.get("fee") or 0),
                liquidity="taker" if r.get("crossed") else "maker",
                ts_ms=int(r["time"]),
                fill_id=str(r.get("tid", "")),
            ))
        return fills

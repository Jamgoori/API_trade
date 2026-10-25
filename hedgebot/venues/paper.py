"""Paper trading on top of a real venue's live market data.

Orders never leave the process. Fills are simulated against the live book:
- IOC walks the L2 book up to the limit price and pays the taker fee.
- ALO (post-only) rests; it fills at its own price once the market trades through it
  (`through`, conservative: best bid drops below a resting buy) or touches it
  (`touch`, optimistic). It is rejected if it would cross on arrival, like the venue.
Margin follows the venue rules the bot cares about: initial margin per position
(Arcus' higher off-hours initial margin for new exposure included), maintenance
margin, funding every hour and liquidation with the venue's liquidation fee.

State is persisted to JSON after every change so a restart keeps the paper book.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from ..models import BBO, AccountState, Fill, OrderState, OrderStatus, Position, Side, Tif
from ..util import D, ONE, ZERO
from .arcus import isolated_liquidation_price
from .base import Venue, VenueError

log = logging.getLogger(__name__)

BBO_CACHE_S = 0.5
DEFAULT_LIQUIDATION_FEE = Decimal("0.01")


@dataclass
class PaperPosition:
    size: Decimal
    entry: Decimal
    isolated_margin: Decimal = ZERO  # > 0 when the position is isolated

    @property
    def isolated(self) -> bool:
        return self.isolated_margin > 0


class PaperVenue(Venue):
    def __init__(self, data: Venue, starting_equity: Decimal, *, state_dir: str | Path | None = None,
                 maker_fill: str = "through", bbo_cache_s: float = BBO_CACHE_S):
        self.data = data
        self.name = data.name
        self.can_trade = True
        self.maker_fill = maker_fill
        self.bbo_cache_s = bbo_cache_s
        self.cash = D(starting_equity)
        self.positions: dict[str, PaperPosition] = {}
        self.orders: dict[str, OrderState] = {}
        self.stops: dict[str, OrderState] = {}
        self.leverage: dict[str, tuple[int, bool]] = {}
        self.fills: list[Fill] = []
        self.fees_paid = ZERO
        self.funding_paid = ZERO
        self.liquidations = 0
        self.last_funding_hour = int(time.time() // 3600)
        self._bbo_cache: dict[str, BBO] = {}
        self.state_path = Path(state_dir) / f"{self.name.replace(':', '_')}.json" if state_dir else None
        self._load()

    # ------------------------------------------------------------ persistence
    def _load(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        raw = json.loads(self.state_path.read_text())
        self.cash = D(raw["cash"])
        self.positions = {s: PaperPosition(D(p["size"]), D(p["entry"]), D(p.get("isolated_margin", 0)))
                          for s, p in raw.get("positions", {}).items()}
        self.leverage = {s: (int(v[0]), bool(v[1])) for s, v in raw.get("leverage", {}).items()}
        self.fees_paid = D(raw.get("fees_paid", 0))
        self.funding_paid = D(raw.get("funding_paid", 0))
        self.liquidations = int(raw.get("liquidations", 0))
        self.last_funding_hour = int(raw.get("last_funding_hour", self.last_funding_hour))
        log.info("%s paper state restored: cash=%s positions=%s", self.name, self.cash, list(self.positions))

    def _save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            "cash": str(self.cash),
            "positions": {s: {"size": str(p.size), "entry": str(p.entry), "isolated_margin": str(p.isolated_margin)}
                          for s, p in self.positions.items()},
            "leverage": {s: [v[0], v[1]] for s, v in self.leverage.items()},
            "fees_paid": str(self.fees_paid),
            "funding_paid": str(self.funding_paid),
            "liquidations": self.liquidations,
            "last_funding_hour": self.last_funding_hour,
        }
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=1))
        tmp.replace(self.state_path)

    # ------------------------------------------------------- market data (pass)
    def refresh_markets(self):
        return self.data.refresh_markets()

    def market(self, symbol):
        return self.data.market(symbol)

    def bbo(self, symbol):
        cached = self._bbo_cache.get(symbol)
        if cached and time.time() - cached.ts < self.bbo_cache_s:
            return cached
        fresh = self.data.bbo(symbol)
        self._bbo_cache[symbol] = fresh
        return fresh

    def book(self, symbol, depth=20):
        return self.data.book(symbol, depth)

    def round_price(self, symbol, price, mode="nearest"):
        return self.data.round_price(symbol, price, mode)

    def round_size(self, symbol, size):
        return self.data.round_size(symbol, size)

    def tick_size(self, symbol, price):
        return self.data.tick_size(symbol, price)

    def funding_history(self, symbol, start_ms, end_ms):
        return self.data.funding_history(symbol, start_ms, end_ms)

    def candles(self, symbol, interval, start_ms, end_ms):
        return self.data.candles(symbol, interval, start_ms, end_ms)

    # ---------------------------------------------------------------- margin
    def _mark(self, symbol: str) -> Decimal:
        try:
            return self.bbo(symbol).mid
        except VenueError:
            return self.market(symbol).mark_price

    def _effective_leverage(self, symbol: str, new_risk: bool) -> Decimal:
        info = self.market(symbol)
        venue_max = info.max_leverage_for_new_risk() if new_risk else info.max_leverage
        chosen = D(self.leverage.get(symbol, (int(info.max_leverage), True))[0])
        return max(ONE, min(chosen, venue_max))

    def _is_isolated(self, symbol: str) -> bool:
        info = self.market(symbol)
        return info.isolated_only or not self.leverage.get(symbol, (0, True))[1]

    def _initial_margin(self, symbol: str, size: Decimal, mark: Decimal, new_risk: bool) -> Decimal:
        return abs(size) * mark / self._effective_leverage(symbol, new_risk)

    def _apply_funding(self) -> None:
        hour = int(time.time() // 3600)
        if hour <= self.last_funding_hour:
            return
        hours = hour - self.last_funding_hour
        for symbol, pos in self.positions.items():
            info = self.market(symbol)
            payment = -pos.size * info.oracle_price * info.funding_rate_1h * hours  # longs pay positive rates
            self.cash += payment
            self.funding_paid -= payment
        self.last_funding_hour = hour
        self._save()

    def _equity(self, marks: dict[str, Decimal]) -> Decimal:
        return self.cash + sum((p.size * (marks[s] - p.entry) for s, p in self.positions.items()), ZERO)

    def _liquidate(self, symbol: str, mark: Decimal, reason: str) -> None:
        pos = self.positions.pop(symbol)
        fee_rate = D(self.market(symbol).extra.get("liquidationFeePpm") or 0) / 1_000_000 or DEFAULT_LIQUIDATION_FEE
        pnl = pos.size * (mark - pos.entry)
        fee = abs(pos.size) * mark * fee_rate
        if pos.isolated:
            pnl = max(pnl, -pos.isolated_margin)  # isolated loss is capped at the posted margin
        self.cash += pnl - fee
        self.fees_paid += fee
        self.liquidations += 1
        log.error("%s PAPER LIQUIDATION %s size=%s mark=%s (%s)", self.name, symbol, pos.size, mark, reason)
        self._save()

    def _check_liquidations(self, marks: dict[str, Decimal]) -> None:
        for symbol, pos in list(self.positions.items()):
            if not pos.isolated:
                continue
            mmf = self.market(symbol).maintenance_margin_fraction
            liq = isolated_liquidation_price(pos.size, pos.entry, pos.isolated_margin, mmf)
            if liq is not None and ((pos.size > 0 and marks[symbol] <= liq) or (pos.size < 0 and marks[symbol] >= liq)):
                self._liquidate(symbol, marks[symbol], f"isolated liq px {liq}")
        cross = {s: p for s, p in self.positions.items() if not p.isolated}
        if not cross:
            return
        isolated_equity = sum((p.isolated_margin + p.size * (marks[s] - p.entry)
                               for s, p in self.positions.items() if p.isolated), ZERO)
        equity = self._equity(marks) - isolated_equity
        maintenance = sum((abs(p.size) * marks[s] * self.market(s).maintenance_margin_fraction
                           for s, p in cross.items()), ZERO)
        if equity < maintenance:
            for symbol in list(cross):
                self._liquidate(symbol, marks[symbol], "cross equity below maintenance")

    # ---------------------------------------------------------------- account
    def account(self) -> AccountState:
        self._apply_funding()
        marks = {s: self._mark(s) for s in self.positions}
        self._check_stops(marks)
        marks = {s: marks[s] for s in self.positions}
        self._check_liquidations(marks)
        marks = {s: marks[s] for s in self.positions}
        positions: dict[str, Position] = {}
        maintenance = ZERO
        initial = ZERO
        for symbol, p in self.positions.items():
            mark = marks[symbol]
            mmf = self.market(symbol).maintenance_margin_fraction
            liq = isolated_liquidation_price(p.size, p.entry, p.isolated_margin, mmf) if p.isolated else None
            positions[symbol] = Position(symbol, p.size, p.entry, mark, liq, "isolated" if p.isolated else "cross",
                                         p.isolated_margin, p.size * (mark - p.entry))
            if p.isolated:
                initial += p.isolated_margin
            else:
                maintenance += abs(p.size) * mark * mmf
                initial += self._initial_margin(symbol, p.size, mark, new_risk=False)
        equity = self._equity(marks)
        return AccountState(self.name, equity, equity - initial, maintenance, positions, time.time())

    # ---------------------------------------------------------------- trading
    def _new_order(self, symbol, side, size, price, tif, reduce_only, client_id) -> OrderState:
        return OrderState(order_id=uuid.uuid4().hex[:16], symbol=symbol, side=side, price=D(price), size=D(size),
                          tif=tif, status=OrderStatus.OPEN, client_id=client_id, reduce_only=reduce_only,
                          created_ts=time.time())

    def _reject(self, order: OrderState, reason: str) -> OrderState:
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        return order

    def _increases_exposure(self, symbol: str, side: Side, size: Decimal) -> bool:
        current = self.positions.get(symbol, PaperPosition(ZERO, ZERO)).size
        return abs(current + side.sign * size) > abs(current)

    def _margin_ok(self, symbol: str, side: Side, size: Decimal, price: Decimal) -> bool:
        if not self._increases_exposure(symbol, side, size):
            return True
        account = self.account()
        return account.free_collateral >= self._initial_margin(symbol, size, price, new_risk=True)

    def place_order(self, symbol, side, size, price, tif, *, reduce_only=False, client_id=None) -> OrderState:
        info = self.market(symbol)
        size, price = D(size), D(price)
        order = self._new_order(symbol, side, size, price, tif, reduce_only, client_id)
        if not info.online:
            return self._reject(order, "MARKET_OFFLINE")
        if size <= 0:
            return self._reject(order, "ZERO_SIZE")
        if not reduce_only and size * price < info.min_notional:
            return self._reject(order, "MIN_NOTIONAL")
        if reduce_only and self._increases_exposure(symbol, side, size):
            return self._reject(order, "REDUCE_ONLY_WOULD_INCREASE")
        if not self._margin_ok(symbol, side, size, price):
            return self._reject(order, "UNDERCOLLATERALIZED")
        if tif is Tif.IOC:
            self._fill_ioc(order)
            return order
        bbo = self.bbo(symbol)
        crosses = price >= bbo.ask if side is Side.BUY else price <= bbo.bid
        if crosses and tif is Tif.ALO:
            return self._reject(order, "POST_ONLY_WOULD_CROSS")
        self.orders[order.order_id] = order
        return order

    def _fill_ioc(self, order: OrderState) -> None:
        book = self.data.book(order.symbol, 20)
        levels = book.asks if order.side is Side.BUY else book.bids
        remaining, cost = order.size, ZERO
        for px, sz in levels:
            if (order.side is Side.BUY and px > order.price) or (order.side is Side.SELL and px < order.price):
                break
            take = min(remaining, sz)
            cost += take * px
            remaining -= take
            if remaining <= 0:
                break
        filled = order.size - remaining
        if filled > 0:
            avg = cost / filled
            self._apply_fill(order, filled, avg, "taker")
            order.filled, order.avg_price = filled, avg
            order.status = OrderStatus.FILLED
        else:
            order.status = OrderStatus.CANCELED
            order.reject_reason = "IOC_CANCELED"

    def _apply_fill(self, order: OrderState, size: Decimal, price: Decimal, liquidity: str) -> None:
        info = self.market(order.symbol)
        fee = size * price * (info.maker_fee if liquidity == "maker" else info.taker_fee)
        signed = order.side.sign * size
        pos = self.positions.get(order.symbol, PaperPosition(ZERO, ZERO))
        new_size = pos.size + signed
        if pos.size == 0 or (pos.size > 0) == (signed > 0):
            # opening / adding: weighted average entry
            entry = (pos.entry * abs(pos.size) + price * size) / abs(new_size)
            realized = ZERO
        else:
            closed = min(abs(pos.size), size)
            realized = closed * (price - pos.entry) * (1 if pos.size > 0 else -1)
            entry = pos.entry if abs(new_size) > 0 and (new_size > 0) == (pos.size > 0) else price
        isolated_margin = ZERO
        if self._is_isolated(order.symbol) and new_size != 0:
            lev = self._effective_leverage(order.symbol, new_risk=False)
            isolated_margin = abs(new_size) * entry / lev
        self.cash += realized - fee
        self.fees_paid += fee
        if new_size == 0:
            self.positions.pop(order.symbol, None)
        else:
            self.positions[order.symbol] = PaperPosition(new_size, entry, isolated_margin)
        order.fee = (order.fee or ZERO) + fee
        self.fills.append(Fill(self.name, order.symbol, order.order_id, order.side, size, price, fee, liquidity,
                               int(time.time() * 1000), uuid.uuid4().hex[:12]))
        self._save()

    def order_state(self, order: OrderState) -> OrderState:
        if order.order_id in self.stops:
            return self.stops[order.order_id]
        live = self.orders.get(order.order_id)
        if live is None:
            return order
        bbo = self.bbo(live.symbol)
        if live.side is Side.BUY:
            hit = bbo.ask <= live.price or (bbo.bid < live.price if self.maker_fill == "through" else bbo.bid <= live.price)
        else:
            hit = bbo.bid >= live.price or (bbo.ask > live.price if self.maker_fill == "through" else bbo.ask >= live.price)
        if hit:
            if live.reduce_only and self._increases_exposure(live.symbol, live.side, live.remaining):
                live.status = OrderStatus.CANCELED
                live.reject_reason = "REDUCE_ONLY_WOULD_INCREASE"
            else:
                self._apply_fill(live, live.remaining, live.price, "maker")
                live.filled, live.avg_price = live.size, live.price
                live.status = OrderStatus.FILLED
            self.orders.pop(live.order_id, None)
        return live

    def cancel_order(self, symbol: str, order_id: str) -> None:
        order = self.orders.pop(order_id, None) or self.stops.pop(order_id, None)
        if order:
            order.status = OrderStatus.CANCELED

    def place_stop(self, symbol, side, size, trigger) -> OrderState:
        order = self._new_order(symbol, side, D(size), D(trigger), Tif.IOC, True, None)
        order.trigger_price = D(trigger)
        current = self.positions.get(symbol)
        if current is None or (current.size > 0) == (side is Side.BUY):
            return self._reject(order, "NO_POSITION_TO_PROTECT")
        self.stops[order.order_id] = order
        return order

    def _check_stops(self, marks: dict[str, Decimal]) -> None:
        """Fire resting stops whose trigger the mark crossed: close the position with a market IOC."""
        for order_id, stop in list(self.stops.items()):
            pos = self.positions.get(stop.symbol)
            if pos is None:
                continue
            mark = marks.get(stop.symbol) or self._mark(stop.symbol)
            hit = mark <= stop.trigger_price if stop.side is Side.SELL else mark >= stop.trigger_price
            if not hit:
                continue
            self.stops.pop(order_id)
            size = min(abs(pos.size), stop.size) if stop.size > 0 else abs(pos.size)
            far = Decimal("0.5") if stop.side is Side.SELL else Decimal("1.5")
            fill = self._new_order(stop.symbol, stop.side, size, mark * far, Tif.IOC, True, None)
            self._fill_ioc(fill)
            stop.filled, stop.avg_price, stop.status = fill.filled, fill.avg_price, OrderStatus.FILLED
            log.error("%s PAPER STOP fired %s %s %s at mark %s", self.name, stop.symbol, stop.side.value, size, mark)

    def cancel_all(self) -> None:
        for order_id in list(self.orders):
            self.cancel_order("", order_id)

    def set_leverage(self, symbol: str, leverage: int, cross: bool) -> None:
        info = self.market(symbol)
        if leverage > info.max_leverage:
            raise VenueError(f"{self.name}: leverage {leverage} above market max {info.max_leverage}")
        self.leverage[symbol] = (int(leverage), cross and not info.isolated_only)
        self._save()

    def recent_fills(self, since_ms: int) -> list[Fill]:
        return [f for f in self.fills if f.ts_ms >= since_ms]

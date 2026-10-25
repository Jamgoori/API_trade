"""Exchange-side stop orders that protect each leg while the bot is not running.

Layering, from first to last line of defence:
    bot de-risk (both legs together)  ->  exchange stop (this module)  ->  venue liquidation

Trigger prices:
- cross account: the uniform adverse move that would bring the account to
  `backstop_margin_ratio` (equity / maintenance), applied to this position's mark.
  The bot's own de-risk fires earlier, at `margin_ratio_critical`.
- isolated position: `backstop_isolated_buffer_pct` before its liquidation price.
  The bot de-risks earlier, at `isolated_liq_buffer_critical_pct`.

Arcus stops are position-level (they always close the whole position); Hyperliquid
stops are sized and get replaced when the position size or trigger drifts.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from .models import AccountState, OrderState, OrderStatus, Position, Side
from .util import ZERO
from .venues.base import VenueError

log = logging.getLogger(__name__)

SIZE_DRIFT = Decimal("0.10")
TRIGGER_DRIFT = Decimal("0.01")
# A leg that only fails after a move this large gets no stop: it adds no protection, and venues
# reject trigger orders priced that far from the oracle (Hyperliquid: 95%). Placed once it comes closer.
MAX_STOP_DISTANCE = Decimal("0.5")


@dataclass
class Backstop:
    order: OrderState
    trigger: Decimal
    size: Decimal
    placed_ts: float


def stop_trigger(acct: AccountState, pos: Position, backstop_ratio: Decimal, isolated_buffer_pct: Decimal) -> Decimal | None:
    """Trigger price for a protective stop on `pos`, or None when it cannot be placed sensibly."""
    if pos.size == 0 or pos.mark_price <= 0:
        return None
    long = pos.size > 0
    if pos.margin_mode == "isolated":
        if pos.liquidation_price is None or pos.liquidation_price <= 0:
            return None
        b = isolated_buffer_pct / 100
        trigger = pos.liquidation_price / (1 - b) if long else pos.liquidation_price / (1 + b)
    else:
        if acct.maintenance_margin <= 0 or acct.gross_notional <= 0:
            return None
        move = (acct.equity - backstop_ratio * acct.maintenance_margin) / acct.gross_notional
        if move <= 0:
            return None
        trigger = pos.mark_price * (1 - move) if long else pos.mark_price * (1 + move)
    # must sit on the protective side of the current mark, otherwise the bot is already de-risking
    if (long and trigger >= pos.mark_price) or (not long and trigger <= pos.mark_price) or trigger <= 0:
        return None
    if abs(trigger - pos.mark_price) / pos.mark_price > MAX_STOP_DISTANCE:
        return None
    return trigger


class BackstopManager:
    def __init__(self, engine):
        self.engine = engine
        self.cfg = engine.cfg
        self.active: dict[tuple[str, str], Backstop] = {}
        self.skipped = False  # a pair was skipped (hedge in flight): the engine retries sooner

    def _legs(self):
        # Busy pairs too: a pair builds or rotates for hours in back-to-back clips, and a reduce-only,
        # position-sized stop is safe mid-clip. Only skipped while a hedge is in flight.
        self.skipped = False
        for ex in self.engine.executors:
            if ex.clip is not None and (ex.clip.hedge_pending > 0 or ex.clip.hedge_order is not None):
                self.skipped = True
                continue
            yield ex, ex.pair.maker.venue, ex.pair.maker.symbol
            yield ex, ex.pair.hedge.venue, ex.pair.hedge.symbol

    def _cancel(self, key: tuple[str, str], wait_s: float = 0) -> bool:
        """Cancel a tracked stop. False (still tracked, retried next refresh) when the cancel failed.

        Arcus cancels asynchronously and allows one position stop per market, so a replacement
        waits up to `wait_s` for the old one to be gone.
        """
        bs = self.active.get(key)
        if bs is None:
            return True
        venue = self.engine.venues[key[0]]
        if not bs.order.status.done:
            try:
                venue.cancel_order(key[1], bs.order.order_id)
                deadline = time.time() + wait_s
                while time.time() < deadline:
                    bs.order = venue.order_state(bs.order)
                    if bs.order.status.done:
                        break
                    time.sleep(0.5)
            except VenueError as exc:
                log.warning("cancel stop %s %s failed: %s", key[0], key[1], exc)
                return False
        self.active.pop(key, None)
        return True

    def refresh(self) -> None:
        r = self.cfg.risk
        for ex, venue_name, symbol in self._legs():
            key = (venue_name, symbol)
            venue = self.engine.venues[venue_name]
            try:
                acct = self.engine.account(venue_name, self.cfg.poll.account_s)
                current = self.active.get(key)
                if current:
                    current.order = venue.order_state(current.order)
                    if current.order.status is OrderStatus.FILLED:
                        self.engine.event("critical", "stop_fired",
                                          f"{venue_name} {symbol}: exchange stop fired at ~{current.trigger:.4f}",
                                          pair=ex.name, venue=venue_name, notify=True)
                    elif current.order.status is OrderStatus.REJECTED:
                        self._rejected(venue_name, symbol, current.order.reject_reason)
                    if current.order.status.done:
                        self.active.pop(key, None)
                        current = None
                pos = acct.positions.get(symbol)
                if pos is None or pos.size == 0:
                    if current:
                        self._cancel(key)
                    continue
                trigger = stop_trigger(acct, pos, r.backstop_margin_ratio, r.backstop_isolated_buffer_pct)
                if trigger is None:
                    continue
                trigger = venue.round_price(symbol, trigger, "nearest")
                size = abs(pos.size)
                if current and not self._stale(current, trigger, size, pos.mark_price, venue_name,
                                               sized_only=ex.busy):
                    continue
                if current and ex.busy and venue_name == "arcus":
                    continue  # Arcus stops resize themselves; trigger drift waits until the clip is done
                if current and not self._cancel(key, wait_s=5 if venue_name == "arcus" else 0):
                    continue
                side = Side.SELL if pos.size > 0 else Side.BUY
                order = venue.place_stop(symbol, side, size, trigger)
                if order.status is OrderStatus.REJECTED:
                    self._rejected(venue_name, symbol, order.reject_reason)
                    continue
                self.active[key] = Backstop(order, order.trigger_price or trigger, size, time.time())
                log.info("%s %s: exchange stop %s %s @ %s", venue_name, symbol, side.value, size,
                         order.trigger_price or trigger)
            except VenueError as exc:
                self.engine.event("warning", "stop_error", f"{venue_name} {symbol}: stop refresh failed: {exc}",
                                  venue=venue_name, throttle_key=f"stoperr:{venue_name}", throttle_s=1800)

    def _rejected(self, venue_name: str, symbol: str, reason: str | None) -> None:
        self.engine.event("warning", "stop_rejected", f"{venue_name} {symbol}: stop rejected: {reason}",
                          venue=venue_name, notify=True, throttle_key=f"stoprej:{venue_name}:{symbol}", throttle_s=3600)

    @staticmethod
    def _stale(bs: Backstop, trigger: Decimal, size: Decimal, mark: Decimal, venue_name: str,
               sized_only: bool = False) -> bool:
        if venue_name != "arcus" and bs.size > 0 and abs(size - bs.size) / bs.size > SIZE_DRIFT:
            return True  # Arcus stops are position-level and resize themselves
        return not sized_only and mark > 0 and abs(trigger - bs.trigger) / mark > TRIGGER_DRIFT

    def cancel_all(self) -> None:
        for key in list(self.active):
            self._cancel(key)

    def status(self) -> dict[str, str]:
        return {f"{v} {s}": f"{bs.order.side.value} @ {bs.trigger:.4f}" for (v, s), bs in self.active.items()}

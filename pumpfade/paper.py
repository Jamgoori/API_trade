"""Paper execution: turn scan results into simulated shorts and follow them to take-profit, stop or time exit.

Every book in Settings.books gets the same signals:
- limit:  (the strategy) a sell limit 0.5% above the 00:00 UTC open, kept for an hour; if the price never trades
          through it, the order is cancelled and the position sells at market at 01:00 UTC (IOC, at most 0.3%
          below the best bid);
- market: the whole position sells at 00:00 UTC into the live order book (same IOC cap);
- half:   half of each.
Take-profit is a resting buy limit (fills when a 1-minute low trades below it), the stop is a stop-market
(fills at the stop or the candle open if it gapped past, plus the slippage of the live book), and whatever is
left at the deadline is bought back at market. Funding is settled from Binance's own funding history.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from .config import Settings
from .fills import buy_qty_from_asks, sell_into_bids
from .signals import DAY_MS, HOUR_MS, Candidate
from .store import Leg, Store

log = logging.getLogger(__name__)

MIN_MS = 60_000
LATE_MS = 5 * MIN_MS  # past this, a scheduled market order is priced from the candle it should have hit


class PaperEngine:
    def __init__(self, client, store: Store, settings: Settings, notify=None, clock=None):
        self.client = client
        self.store = store
        self.settings = settings
        self.st = settings.strategy
        self.notify = notify or (lambda msg, **kw: log.info(msg))
        self.clock = clock or (lambda: int(time.time() * 1000))

    # ------------------------------------------------------------------ entries

    def open_signals(self, cands: list[Candidate], day: int) -> list[str]:
        now = self.clock()
        entry_ms = (day + 1) * DAY_MS
        notes = []
        for c in [c for c in cands if c.passed]:
            ref = self._hour_open(c.symbol, entry_ms)
            book_snapshot = None
            for spec in self.settings.books:
                active = [lg for lg in self.store.active_legs() if lg.book == spec.name]
                if self.store.is_banned(spec.name, c.symbol):
                    self.store.log_skip(day, spec.name, c.symbol, "dropped after an earlier stop")
                    continue
                if any(lg.symbol == c.symbol for lg in active):
                    self.store.log_skip(day, spec.name, c.symbol, "already holding this coin")
                    continue
                equity = self.store.book_equity(spec.name, self.settings.start_equity)
                margin = equity * self.settings.margin_fraction
                used = sum(lg.notional for lg in active)
                if used + margin > self.settings.max_margin_used * equity:
                    self.store.log_skip(day, spec.name, c.symbol, "margin limit reached")
                    continue
                parts = {"market": [("A", "market", 1.0)], "limit": [("B", "limit", 1.0)],
                         "half": [("A", "market", 0.5), ("B", "limit", 0.5)]}[spec.entry]
                for part, kind, share in parts:
                    leg = Leg(book=spec.name, symbol=c.symbol, signal_day=day, part=part, kind=kind, status="pending",
                              margin=margin, notional=margin * share, ref_px=ref, hold_hours=spec.hold_hours)
                    if kind == "market":
                        if book_snapshot is None:
                            book_snapshot = self.client.depth(c.symbol)
                        self._enter_market(leg, now, book_snapshot)
                    else:
                        leg.limit_px = ref * (1 + self.st.limit_offset)
                        leg.limit_until_ms = entry_ms + self.st.limit_window_hours * HOUR_MS
                        leg.checked_ms = entry_ms - MIN_MS
                    self.store.save_leg(leg)
                notes.append(f"{spec.name}: short {c.symbol} margin ${margin:,.0f}")
        return notes

    def _hour_open(self, symbol: str, entry_ms: int) -> float:
        rows = self.client.klines(symbol, "1h", start_ms=entry_ms, limit=1)
        if rows and int(rows[0][0]) == entry_ms:
            return float(rows[0][1])
        bids, asks = self.client.depth(symbol, 5)
        return (bids[0][0] + asks[0][0]) / 2

    def _enter_market(self, leg: Leg, now: int, snapshot=None) -> None:
        bids, asks = snapshot or self.client.depth(leg.symbol)
        f = sell_into_bids(bids, asks, leg.notional, self.st.max_slippage)
        if f.qty <= 0:
            leg.status = "cancelled"
            leg.exit_reason = "nothing filled within the slippage cap"
            return
        leg.fill_ratio = f.notional / leg.notional
        leg.notional = f.notional
        leg.fees += f.notional * self.st.taker_fee
        self._arm(leg, f.vwap, f.qty, now, slip=f.slippage)

    def _arm(self, leg: Leg, px: float, qty: float, ms: int, slip: float | None) -> None:
        leg.status = "open"
        leg.entry_px, leg.qty, leg.entry_ms, leg.entry_slip = px, qty, ms, slip
        leg.tp_px = px * (1 - self.st.take_profit)
        leg.stop_px = px * (1 + self.st.stop)
        leg.deadline_ms = ms + leg.hold_hours * HOUR_MS
        leg.checked_ms = ms // MIN_MS * MIN_MS
        leg.funding_ms = ms

    # ------------------------------------------------------------------ monitoring

    def tick(self) -> None:
        """Process every 1-minute candle since the last tick for all pending and open legs."""
        now = self.clock()
        by_symbol: dict[str, list[Leg]] = defaultdict(list)
        for leg in self.store.active_legs():
            by_symbol[leg.symbol].append(leg)
        for sym, legs in by_symbol.items():
            try:
                candles = self._candles(sym, min(lg.checked_ms or 0 for lg in legs) + MIN_MS, now)
                for leg in legs:
                    self._process(leg, candles, now)
            except Exception as exc:
                log.exception("tick failed for %s", sym)
                self.notify(f"paper tick failed for {sym}: {exc}", level="warning", key=f"tick-{sym}", throttle_s=3600)

    def _candles(self, symbol: str, start_ms: int, now: int) -> list[tuple[int, float, float, float, float]]:
        out, start = [], start_ms
        while start < now - MIN_MS:
            rows = self.client.klines(symbol, "1m", start_ms=start, end_ms=now, limit=1000)
            closed = [r for r in rows if int(r[6]) < now]
            out += [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in closed]
            if len(rows) < 1000 or not closed:
                break
            start = int(closed[-1][0]) + MIN_MS
        return out

    def _process(self, leg: Leg, candles, now: int) -> None:
        for t, o, h, lo, c in candles:
            if leg.checked_ms is not None and t <= leg.checked_ms:
                continue
            if leg.status == "pending":
                if t >= leg.limit_until_ms:
                    break
                if h > leg.limit_px:  # traded through the resting sell
                    px = max(leg.limit_px, o)
                    leg.fees += leg.notional * self.st.maker_fee
                    self._arm(leg, px, leg.notional / px, t, slip=None)
                    leg.fill_ratio = 1.0
                    leg.checked_ms = t
                    if h >= leg.stop_px:  # the take-profit cannot fill in the fill candle, the stop can
                        self._stop_out(leg, t, o, now)
                        break
                    continue
            elif leg.status == "open":
                if t >= leg.deadline_ms:
                    break
                if h >= leg.stop_px:  # stop first when one candle touches both
                    self._stop_out(leg, t, o, now)
                    break
                if lo < leg.tp_px:
                    px = min(leg.tp_px, o)
                    self._close(leg, px, t + MIN_MS - 1, "take-profit", self.st.maker_fee, 0.0)
                    break
            leg.checked_ms = t
        if leg.status == "pending" and now >= leg.limit_until_ms:
            leg.kind = "fallback"
            if now - leg.limit_until_ms <= LATE_MS:
                self._enter_market(leg, now)
            else:  # the bot was down: price it from the candle it should have sold into
                px = self._open_at(leg.symbol, leg.limit_until_ms) or leg.ref_px
                slip = self._sell_slippage(leg.symbol, leg.notional)
                leg.fill_ratio = 1.0
                leg.fees += leg.notional * self.st.taker_fee
                self._arm(leg, px * (1 - slip), leg.notional / (px * (1 - slip)), leg.limit_until_ms, slip=slip)
        if leg.status == "open" and now >= leg.deadline_ms:
            bids, asks = self.client.depth(leg.symbol)
            f = buy_qty_from_asks(bids, asks, leg.qty)
            if now - leg.deadline_ms <= LATE_MS:
                self._close(leg, f.vwap, now, "time", self.st.taker_fee, f.slippage)
            else:
                px = (self._open_at(leg.symbol, leg.deadline_ms) or f.mid) * (1 + f.slippage)
                self._close(leg, px, leg.deadline_ms, "time", self.st.taker_fee, f.slippage)
        self.store.save_leg(leg)

    def _stop_out(self, leg: Leg, t: int, o: float, now: int) -> None:
        bids, asks = self.client.depth(leg.symbol)
        slip = buy_qty_from_asks(bids, asks, leg.qty).slippage
        px = max(leg.stop_px, o) * (1 + slip)
        self._close(leg, px, t + MIN_MS - 1, "stop", self.st.taker_fee, slip)

    def _open_at(self, symbol: str, ms: int) -> float | None:
        rows = self.client.klines(symbol, "1m", start_ms=ms // MIN_MS * MIN_MS, limit=1)
        return float(rows[0][1]) if rows else None

    def _sell_slippage(self, symbol: str, notional: float) -> float:
        bids, asks = self.client.depth(symbol)
        return sell_into_bids(bids, asks, notional, self.st.max_slippage).slippage

    # ------------------------------------------------------------------ exits and funding

    def settle_funding(self, leg: Leg, upto_ms: int) -> None:
        if leg.entry_ms is None or (leg.funding_ms or 0) >= upto_ms:
            return
        rows = self.client.funding_history(leg.symbol, start_ms=(leg.funding_ms or leg.entry_ms) + 1,
                                           end_ms=upto_ms, limit=100)
        for r in rows:
            ft = int(r["fundingTime"])
            if leg.entry_ms < ft <= upto_ms:
                mark = float(r.get("markPrice") or 0) or leg.entry_px
                leg.funding += float(r["fundingRate"]) * leg.qty * mark  # a short receives positive funding
                leg.funding_ms = ft
        leg.funding_ms = max(leg.funding_ms or 0, min(upto_ms, self.clock()))

    def settle_all_funding(self) -> None:
        now = self.clock()
        for leg in self.store.legs("status='open'"):
            try:
                self.settle_funding(leg, now)
                self.store.save_leg(leg)
            except Exception as exc:
                log.warning("funding update failed for %s: %s", leg.symbol, exc)

    def _close(self, leg: Leg, px: float, ms: int, reason: str, fee_rate: float, slip: float) -> None:
        self.settle_funding(leg, ms)
        leg.exit_px, leg.exit_ms, leg.exit_reason, leg.exit_slip = px, ms, reason, slip
        leg.fees += leg.qty * px * fee_rate
        leg.pnl = leg.qty * (leg.entry_px - px) - leg.fees + leg.funding
        leg.status = "closed"
        equity = self.store.add_pnl(leg.book, leg.pnl, ms)
        if reason == "stop":
            self.store.ban(leg.book, leg.symbol, leg.signal_day)
        self.notify(f"[{leg.book}] {leg.symbol} {leg.part} closed by {reason}: "
                    f"{leg.entry_px:.6g} → {px:.6g}, PnL ${leg.pnl:+,.2f} (book ${equity:,.0f})")

"""Live execution of research/STRATEGY.md on a Binance USD-M account (one-way mode, USDT, isolated 1x).

Each signal becomes one position tracked in the `live_positions` table and driven by `tick()`, which only relies
on what the exchange reports (order status by client id, position amounts, fills). That makes it restart-safe:
a fresh process with the same database picks up where the last one stopped.

Main rule:   sell limit at 09:00 KST open +0.5% (GTC) → at 10:00 KST cancel the rest and sell it IOC within 0.3%
             of the best bid.
Listing rule: sell IOC within 0.3% of the best bid at the scheduled hour.
Both:        as soon as anything is filled, a STOP_MARKET (+15%) and a TAKE_PROFIT_MARKET (−15%) closePosition
             algo order protect the whole position; after 48h the position is bought back at market.
             If the stop cannot be placed, the position is closed immediately.
Risk:        12% of equity per signal (5% while starting), at most 90% of equity as margin; below −25% from the
             equity peak the size drops to two thirds; at −35%, or when the last 100 trades average below −1.5%,
             new entries stop until `pumpfade live --resume`.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, fields
from decimal import Decimal

from .config import Settings
from .signals import DAY_MS, HOUR_MS, Candidate
from .store import Store
from .trader import avg_fill_price, fmt

log = logging.getLogger(__name__)
MIN_MS = 60_000
LATE_MS = 5 * MIN_MS
FLAT_CONFIRM_MS = 25_000      # a position must read as zero this long before it counts as closed
FALLBACK_LATE_MS = 30 * MIN_MS  # after a restart, skip the 10:00 KST IOC if it is this late
BOOK = "live"


@dataclass
class LivePos:
    key: str
    symbol: str
    sleeve: str                 # main | listing
    signal_day: int
    status: str                 # entering | open | closing | closed | failed
    margin: float
    qty_target: float
    ref_px: float
    hold_hours: int
    limit_px: float | None = None
    limit_until_ms: int | None = None
    entry_cid: str | None = None
    fb_cid: str | None = None
    qty: float = 0.0
    limit_filled: float = 0.0
    entry_avg: float | None = None
    entry_ms: int | None = None
    tp_px: float | None = None
    sl_px: float | None = None
    tp_cid: str | None = None
    sl_cid: str | None = None
    deadline_ms: int | None = None
    close_cid: str | None = None
    close_sent_ms: int | None = None
    exit_reason: str | None = None
    exit_avg: float | None = None
    exit_ms: int | None = None
    pnl: float | None = None
    fees: float = 0.0
    funding: float = 0.0
    note: str = ""
    created_ms: int = 0
    flat_ms: int | None = None  # first time the exchange showed no position (cleared when it shows one again)
    alerted_qty: float = 0.0    # quantity already announced on Telegram


POS_FIELDS = [f.name for f in fields(LivePos)]
ACTIVE = ("entering", "open", "closing")


class LiveBook:
    def __init__(self, store: Store):
        self.db = store.db
        self.db.execute(f"CREATE TABLE IF NOT EXISTS live_positions ({', '.join(POS_FIELDS)}, PRIMARY KEY (key))")
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(live_positions)").fetchall()}
        for name in POS_FIELDS:
            if name not in have:
                self.db.execute(f"ALTER TABLE live_positions ADD COLUMN {name}")
        self.db.commit()

    def save(self, p: LivePos) -> None:
        d = asdict(p)
        self.db.execute(f"INSERT OR REPLACE INTO live_positions ({', '.join(d)}) VALUES ({', '.join('?' * len(d))})",
                        list(d.values()))
        self.db.commit()

    def get(self, key: str) -> LivePos | None:
        r = self.db.execute("SELECT * FROM live_positions WHERE key=?", (key,)).fetchone()
        return LivePos(**{k: r[k] for k in POS_FIELDS}) if r else None

    def all(self, statuses: tuple[str, ...] | None = None) -> list[LivePos]:
        rows = self.db.execute("SELECT * FROM live_positions ORDER BY created_ms").fetchall()
        out = [LivePos(**{k: r[k] for k in POS_FIELDS}) for r in rows]
        return [p for p in out if statuses is None or p.status in statuses]


class LiveEngine:
    def __init__(self, trader, public, store: Store, settings: Settings, notify=None, clock=None):
        self.tr = trader
        self.pub = public
        self.store = store
        self.book = LiveBook(store)
        self.settings = settings
        self.st = settings.strategy
        self.notify = notify or (lambda msg, **kw: log.info(msg))
        self.clock = clock or (lambda: int(time.time() * 1000))
        self._account_cache: tuple[int, dict] | None = None

    # ------------------------------------------------------------------ account, risk and sizing

    def _account(self, fresh: bool = False) -> dict:
        now = self.clock()
        if fresh or self._account_cache is None or now - self._account_cache[0] > 20_000:
            self._account_cache = (now, self.tr.account())
        return self._account_cache[1]

    def _positions(self, fresh: bool = False) -> dict[str, float]:
        out = {}
        for p in self._account(fresh).get("positions", []):
            amt = float(p.get("positionAmt") or 0)
            if amt:
                out[p["symbol"]] = out.get(p["symbol"], 0.0) + amt
        return out

    def equity(self) -> float:
        return float(self._account()["totalMarginBalance"])

    def risk_state(self) -> dict:
        """Current margin fraction and whether new entries are allowed (updates the equity peak)."""
        eq = self.equity()
        peak = max(self.store.get("live_peak") or eq, eq)
        self.store.put("live_peak", peak)
        dd = eq / peak - 1
        base = self.settings.live_margin_fraction
        was = bool(self.store.get("live_reduced"))
        # cut at −25%, restore only once the equity makes a new high
        reduced = dd <= -self.settings.live_reduce_drawdown or (was and dd < -1e-9)
        if reduced != was:
            self.store.put("live_reduced", reduced)
            self.notify(f"drawdown {dd:+.1%}: size {'reduced to' if reduced else 'back to'} "
                        f"{base * (2 / 3 if reduced else 1):.1%} per signal", level="warning")
        halted = self.store.get("live_halted")
        if not halted and dd <= -self.settings.live_halt_drawdown:
            halted = f"drawdown {dd:+.1%} from the equity peak"
        closed = [p for p in self.book.all(("closed",)) if p.pnl is not None and p.margin]
        if not halted and len(closed) >= 100:
            mean = sum(p.pnl / p.margin for p in closed[-100:]) / 100
            if mean < -0.015:
                halted = f"last 100 trades averaged {mean:+.2%}"
        if halted and not self.store.get("live_halted"):
            self.store.put("live_halted", halted)
            self.notify(f"NEW ENTRIES STOPPED: {halted}. Check the bot, then run `pumpfade live --resume`.",
                        level="critical")
        return {"equity": eq, "peak": peak, "dd": dd, "fraction": base * (2 / 3 if reduced else 1), "halted": halted}

    def _can_open(self, symbol: str, day: int, label: str) -> tuple[float, str]:
        """(margin, "") or (0, reason)."""
        rs = self.risk_state()
        if rs["halted"]:
            return 0.0, f"halted: {rs['halted']}"
        active = self.book.all(ACTIVE)
        if any(p.symbol == symbol for p in active):
            return 0.0, "already holding this coin"
        if symbol in self._positions(fresh=True):
            return 0.0, "the account already has a position in this coin"
        margin = rs["equity"] * rs["fraction"]
        if sum(p.margin for p in active) + margin > self.settings.max_margin_used * rs["equity"] * (1 + 1e-9):
            return 0.0, "margin limit reached"
        return margin, ""

    # ------------------------------------------------------------------ entries

    def open_signals(self, cands: list[Candidate], day: int) -> list[str]:
        notes = []
        entry_ms = (day + 1) * DAY_MS
        for c in [c for c in cands if c.passed]:
            if self.store.is_banned(BOOK, c.symbol):
                notes.append(f"{c.symbol}: skipped, dropped after an earlier stop")
                continue
            margin, why = self._can_open(c.symbol, day, "main")
            if why:
                notes.append(f"{c.symbol}: skipped, {why}")
                self.store.log_skip(day, BOOK, c.symbol, why)
                continue
            try:
                notes.append(self._open_main(c.symbol, day, entry_ms, margin))
            except Exception as exc:
                log.exception("main entry failed for %s", c.symbol)
                notes.append(f"{c.symbol}: entry failed: {exc}")
        return notes

    def _open_main(self, symbol: str, day: int, entry_ms: int, margin: float) -> str:
        f = self.tr.filters(symbol)
        ref = self._hour_open(symbol, entry_ms)
        limit = f.price(ref * (1 + self.st.limit_offset), up=True)
        qty = f.qty(margin / float(limit))
        if qty < f.min_qty or qty * limit < f.min_notional:
            return f"{symbol}: skipped, ${margin:,.0f} is below the exchange minimum"
        if not self._clear_algos(symbol):
            return f"{symbol}: skipped, old stop/take-profit orders on this coin could not be removed"
        self.tr.prepare_symbol(symbol, 1)
        key = f"m-{day}-{symbol}"
        p = LivePos(key=key, symbol=symbol, sleeve="main", signal_day=day, status="entering", margin=margin,
                    qty_target=float(qty), ref_px=ref, hold_hours=48, limit_px=float(limit),
                    limit_until_ms=entry_ms + self.st.limit_window_hours * HOUR_MS,
                    entry_cid=f"pf-{symbol}-{day}-E"[:36], created_ms=self.clock())
        self.book.save(p)  # saved first: a crash after sending is found again by client id
        try:
            self.tr.new_order(symbol=symbol, side="SELL", type="LIMIT", timeInForce="GTC", quantity=qty,
                              price=limit, newClientOrderId=p.entry_cid)
        except Exception:
            if self.tr.query_order(symbol, p.entry_cid) is None:
                p.status, p.note = "failed", "limit order was rejected"
                self.book.save(p)
                raise
        return f"{symbol}: sell limit {fmt(qty)} @ {fmt(limit)} (+{self.st.limit_offset:.1%}), margin ${margin:,.0f}"

    def open_listing(self, symbol: str, venue: str, entry_ms: int) -> list[str]:
        day = entry_ms // DAY_MS - 1
        margin, why = self._can_open(symbol, day, "listing")
        if why:
            self.store.log_skip(day, BOOK, symbol, f"listing on {venue}: {why}")
            return [f"{symbol}: listing short skipped, {why}"]
        try:
            if not self._clear_algos(symbol):
                return [f"{symbol}: listing short skipped, old stop/take-profit orders could not be removed"]
            self.tr.prepare_symbol(symbol, 1)
            bid, ask = self.tr.book_top(symbol)
            f = self.tr.filters(symbol)
            qty = f.qty(margin / bid)
            p = LivePos(key=f"l-{venue}-{symbol}-{entry_ms // HOUR_MS}", symbol=symbol, sleeve="listing",
                        signal_day=day, status="entering", margin=margin, qty_target=float(qty),
                        ref_px=(bid + ask) / 2, hold_hours=48, created_ms=self.clock())
            self.book.save(p)
            self._ioc_sell(p, qty)
            if p.qty <= 0:
                p.status, p.note = "failed", "IOC sell found no bids within the slippage cap"
            self.book.save(p)
            return [f"{symbol}: listing short ({venue}) {fmt(p.qty)} @ {p.entry_avg or 0:.6g}" if p.qty
                    else f"{symbol}: listing short ({venue}) not filled"]
        except Exception as exc:
            log.exception("listing entry failed for %s", symbol)
            return [f"{symbol}: listing entry failed: {exc}"]

    def _hour_open(self, symbol: str, entry_ms: int) -> float:
        rows = self.pub.klines(symbol, "1h", start_ms=entry_ms, limit=1)
        if rows and int(rows[0][0]) == entry_ms:
            return float(rows[0][1])
        bid, ask = self.tr.book_top(symbol)
        return (bid + ask) / 2

    def _ioc_sell(self, p: LivePos, qty: Decimal) -> None:
        f = self.tr.filters(p.symbol)
        bid, _ = self.tr.book_top(p.symbol)
        price = f.price(bid * (1 - self.st.max_slippage), up=True)
        p.fb_cid = f"pf-{p.symbol}-{p.signal_day}-F{self.clock() // MIN_MS % 1000}"[:36]
        self.book.save(p)
        try:
            res = self.tr.new_order(symbol=p.symbol, side="SELL", type="LIMIT", timeInForce="IOC", quantity=qty,
                                    price=price, newClientOrderId=p.fb_cid)
        except Exception:
            res = self.tr.query_order(p.symbol, p.fb_cid)
            if res is None:
                raise
        got = float(res.get("executedQty") or 0)
        if got > 0:
            self._add_fill(p, got, avg_fill_price(self.tr, p.symbol, res, p.fb_cid) or float(price))
            p.status = "open"

    # ------------------------------------------------------------------ fills and protection

    def _add_fill(self, p: LivePos, qty: float, avg: float) -> None:
        total = p.qty + qty
        p.entry_avg = ((p.entry_avg or 0) * p.qty + avg * qty) / total
        p.qty = total
        if p.entry_ms is None:
            p.entry_ms = self.clock()
            p.deadline_ms = p.entry_ms + p.hold_hours * HOUR_MS
        self._protect(p)
        if p.alerted_qty == 0 and p.status != "closing":
            self._entry_alert(p)

    def _entry_alert(self, p: LivePos) -> None:
        """Announce a new short (first fill) and, once the entry is done, any quantity added after it."""
        if p.qty <= p.alerted_qty + 1e-12:
            return
        from datetime import datetime, timedelta, timezone
        exit_at = datetime.fromtimestamp((p.deadline_ms or 0) / 1000, timezone(timedelta(hours=9)))
        head = "SHORT opened" if p.alerted_qty == 0 else "entry complete"
        self.notify(f"📉 [live/{p.sleeve}] {p.symbol} {head}: {p.qty:g} @ {p.entry_avg or 0:.6g} "
                    f"(${p.qty * (p.entry_avg or 0):,.0f}), stop {p.sl_px or 0:.6g} (+15%), "
                    f"TP {p.tp_px or 0:.6g} (−15%), time exit {exit_at:%m-%d %H:%M} KST")
        p.alerted_qty = p.qty

    def _protect(self, p: LivePos) -> None:
        """Place (or move) the closePosition stop and take-profit around the current average entry."""
        f = self.tr.filters(p.symbol)
        sl = f.price(p.entry_avg * (1 + self.st.stop), up=True)
        tp = f.price(p.entry_avg * (1 - self.st.take_profit), up=False)
        if p.sl_px and abs(float(sl) / p.sl_px - 1) < 0.002 and p.sl_cid:
            return  # already protected at (about) this level
        old = (p.sl_cid, p.tp_cid)
        n = self.clock() // 1000 % 100000
        p.sl_cid, p.tp_cid = f"pf-{p.symbol}-S{n}"[:36], f"pf-{p.symbol}-T{n}"[:36]
        try:
            self.tr.new_algo(symbol=p.symbol, side="BUY", type="STOP_MARKET", triggerPrice=sl, closePosition="true",
                             workingType="CONTRACT_PRICE", clientAlgoId=p.sl_cid)
        except Exception as exc:
            self.notify(f"{p.symbol}: STOP could not be placed ({exc}); closing the position now", level="critical")
            p.sl_cid = None
            self.book.save(p)
            self._close_now(p, "no stop")
            return
        p.sl_px = float(sl)
        try:
            self.tr.new_algo(symbol=p.symbol, side="BUY", type="TAKE_PROFIT_MARKET", triggerPrice=tp,
                             closePosition="true", workingType="CONTRACT_PRICE", clientAlgoId=p.tp_cid)
            p.tp_px = float(tp)
        except Exception as exc:
            self.notify(f"{p.symbol}: take-profit could not be placed ({exc}); the stop is in place", level="warning")
            p.tp_cid = None
        for cid in old:
            if cid:
                self.tr.cancel_algo(cid)
        self.book.save(p)

    # ------------------------------------------------------------------ the loop

    def tick(self) -> None:
        now = self.clock()
        active = self.book.all(ACTIVE)
        positions = self._positions(fresh=True)
        tracked = {p.symbol for p in active}
        unknown = sorted(set(positions) - tracked)
        if unknown and self.store.get("live_unknown") != unknown:
            self.store.put("live_unknown", unknown)
            self.notify(f"positions the bot did not open (left alone): {', '.join(unknown)}", level="warning")
        for p in active:
            try:
                self._step(p, positions.get(p.symbol, 0.0), now)
            except Exception as exc:
                log.exception("live step failed for %s", p.symbol)
                self.notify(f"{p.symbol}: live step failed: {exc}", level="error", key=f"live-{p.key}",
                            throttle_s=1800)
            self.book.save(p)

    def _flat(self, p: LivePos, amt: float, now: int) -> bool:
        """True once the position has read as zero for FLAT_CONFIRM_MS (guards against a stale account read)."""
        if amt:
            p.flat_ms = None
            return False
        if p.flat_ms is None:
            p.flat_ms = now
        return now - p.flat_ms >= FLAT_CONFIRM_MS

    def _step(self, p: LivePos, amt: float, now: int) -> None:
        if p.status == "entering" and p.sleeve == "main":
            if p.qty > 0 and self._flat(p, amt, now):  # partly filled, then stopped or took profit inside the hour
                self.tr.cancel_order(p.symbol, p.entry_cid)
                self._finalize(p, now)
                return
            o = self.tr.query_order(p.symbol, p.entry_cid)
            self._take_limit_fills(p, o)
            if o and o["status"] == "FILLED":
                p.status = "open"
                self._entry_alert(p)
                return
            if now < p.limit_until_ms:
                return
            if o and o["status"] in ("NEW", "PARTIALLY_FILLED"):
                self.tr.cancel_order(p.symbol, p.entry_cid)
                self._take_limit_fills(p, self.tr.query_order(p.symbol, p.entry_cid) or o)
            f = self.tr.filters(p.symbol)
            remaining = f.qty(p.qty_target - p.qty)
            bid, _ = self.tr.book_top(p.symbol)
            if now - p.limit_until_ms > FALLBACK_LATE_MS:
                p.note = "the 10:00 KST IOC was skipped: the bot was not running at 10:00"
            elif remaining >= f.min_qty and float(remaining) * bid >= float(f.min_notional):
                self._ioc_sell(p, remaining)
            if p.qty <= 0:
                p.status, p.note = "failed", p.note or "neither the limit nor the 10:00 KST IOC filled"
                self.notify(f"⚪ [live/main] {p.symbol}: no position ({p.note})")
            else:
                p.status = "open"
                self._entry_alert(p)
            return
        if p.status == "entering":  # listing entries resolve in open_listing; anything left here failed
            p.status = "failed" if p.qty <= 0 else "open"
            return
        if p.status == "open":
            if self._flat(p, amt, now):
                self._finalize(p, now)
            elif not amt:
                return
            elif now >= p.deadline_ms:
                self._close_now(p, "time")
            return
        if p.status == "closing":
            if self._flat(p, amt, now):
                self._finalize(p, now)
            elif not amt:
                return
            elif now - (p.close_sent_ms or 0) > 2 * MIN_MS:
                self.notify(f"{p.symbol}: still {amt} after the close order, retrying", level="warning")
                self._close_now(p, p.exit_reason or "time")

    def _take_limit_fills(self, p: LivePos, o: dict | None) -> None:
        filled = float(o["executedQty"]) if o else 0.0
        new = filled - p.limit_filled
        if new > 1e-12:
            p.limit_filled = filled
            self._add_fill(p, new, avg_fill_price(self.tr, p.symbol, o, p.entry_cid) or p.limit_px)

    def _close_now(self, p: LivePos, reason: str) -> None:
        for cid in (p.sl_cid, p.tp_cid):
            if cid:
                self.tr.cancel_algo(cid)
        amt = self._positions(fresh=True).get(p.symbol, 0.0)
        p.exit_reason = reason
        p.status = "closing"
        p.close_sent_ms = self.clock()
        if amt:
            f = self.tr.filters(p.symbol)
            p.close_cid = f"pf-{p.symbol}-X{self.clock() // 1000 % 100000}"[:36]
            self.tr.new_order(symbol=p.symbol, side="BUY", type="MARKET", quantity=f.qty(abs(amt)),
                              reduceOnly="true", newClientOrderId=p.close_cid)
        self.book.save(p)

    def _clear_algos(self, symbol: str) -> bool:
        """Remove every conditional order left on the symbol and check that none remain."""
        try:
            if not self.tr.open_algos(symbol):
                return True
            self.tr.cancel_all_algos(symbol)
            for a in self.tr.open_algos(symbol):
                self.tr.cancel_algo(algo_id=a.get("algoId"))
            return not self.tr.open_algos(symbol)
        except Exception:
            log.exception("could not clear the conditional orders on %s", symbol)
            return False

    def _finalize(self, p: LivePos, now: int) -> None:
        if not self._clear_algos(p.symbol):
            self.notify(f"{p.symbol}: stop/take-profit orders may still be open after the close. Cancel them in the "
                        f"Binance app; the bot will not trade this coin until they are gone", level="critical")
        try:
            self.tr.cancel_all_orders(p.symbol)
        except Exception:
            pass
        trades = self.tr.user_trades(p.symbol, (p.entry_ms or p.created_ms) - 5 * MIN_MS)
        buys = [t for t in trades if t["side"] == "BUY"]
        bq = sum(float(t["qty"]) for t in buys)
        p.exit_avg = sum(float(t["qty"]) * float(t["price"]) for t in buys) / bq if bq else None
        p.exit_ms = max((int(t["time"]) for t in buys), default=now)
        realized = sum(float(t.get("realizedPnl") or 0) for t in trades)
        p.fees = sum(float(t.get("commission") or 0) for t in trades if t.get("commissionAsset") == "USDT")
        try:
            p.funding = self.tr.funding_income(p.symbol, p.entry_ms or p.created_ms)
        except Exception:
            p.funding = 0.0
        p.pnl = realized - p.fees + p.funding
        if p.exit_reason is None and p.exit_avg and p.entry_avg:
            move = p.exit_avg / p.entry_avg - 1
            p.exit_reason = ("stop" if move >= self.st.stop / 2 else
                             "take-profit" if move <= -self.st.take_profit / 2 else "other")
        p.status = "closed"
        self.book.save(p)
        if p.exit_reason == "stop" and p.sleeve == "main":
            self.store.ban(BOOK, p.symbol, p.signal_day)
        self.notify(f"[live/{p.sleeve}] {p.symbol} closed by {p.exit_reason}: {p.entry_avg or 0:.6g} → "
                    f"{p.exit_avg or 0:.6g}, PnL ${p.pnl:+,.2f} ({p.pnl / p.margin:+.1%} of margin)")

    def settle_all_funding(self) -> None:
        """Funding is read from Binance's income history when a position closes."""

    def flatten(self) -> list[str]:
        """Emergency: cancel the bot's orders, buy back every position it opened and stop new entries."""
        self.store.put("live_halted", "manual flatten")
        notes = []
        for p in self.book.all(ACTIVE):
            try:
                if p.entry_cid:
                    self.tr.cancel_order(p.symbol, p.entry_cid)
                self._close_now(p, "manual flatten")
                notes.append(f"{p.symbol}: closing")
            except Exception as exc:
                notes.append(f"{p.symbol}: flatten failed: {exc}")
        return notes

    def report(self) -> str:
        rs = self.risk_state()
        closed = self.book.all(("closed",))
        lines = [f"live equity ${rs['equity']:,.2f} (peak ${rs['peak']:,.2f}, {rs['dd']:+.1%}), size {rs['fraction']:.1%}"
                 + (f", HALTED: {rs['halted']}" if rs["halted"] else "")]
        for sleeve in ("main", "listing"):
            c = [p for p in closed if p.sleeve == sleeve and p.pnl is not None and p.margin]
            if not c:
                continue
            r = [p.pnl / p.margin for p in c]
            reasons = {k: sum(p.exit_reason == k for p in c) / len(c) for k in ("take-profit", "stop", "time")}
            lines.append(f"{sleeve}: {len(c)} closed, per trade {sum(r) / len(r):+.2%}, win {sum(x > 0 for x in r) / len(r):.0%}, "
                         f"TP/stop/time {reasons['take-profit']:.0%}/{reasons['stop']:.0%}/{reasons['time']:.0%}, "
                         f"PnL ${sum(p.pnl for p in c):+,.2f}")
        mains = [p for p in self.book.all() if p.sleeve == "main" and p.status in ("open", "closing", "closed")]
        if mains:
            by_limit = sum(1 for p in mains if p.entry_avg and p.limit_px and abs(p.entry_avg / p.limit_px - 1) < 0.001)
            lines.append(f"main entries filled at the +0.5% limit: {by_limit}/{len(mains)}")
        for p in self.book.all(ACTIVE):
            lines.append(f"  open {p.symbol} {p.sleeve} {p.status} qty {p.qty:g} entry {p.entry_avg or 0:.6g} "
                         f"stop {p.sl_px or 0:.6g} TP {p.tp_px or 0:.6g}")
        return "\n".join(lines)

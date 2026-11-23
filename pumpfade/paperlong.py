"""Paper-only longs on red days for the majors (research README 30): never sends an order.

Rule: on a 09:00 KST scan where the BTC/ETH/SOL average day move is below −3%, every coin up 20%+ (any volume rank)
is "bought" at the 09:00 open (+0.3% slippage). Exits: take-profit +8%, stop −30% (a bar touching both counts as the
stop), or the close 72 hours after entry. Fees: 0.05% in, 0.02% on take-profit, 0.05% otherwise. One position per coin.
Results are recorded in the `paper_longs` table and announced on Telegram.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .signals import DAY_MS, HOUR_MS

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))
MAJORS_BELOW = -0.03
TP, STOP, HOLD_H = 0.08, 0.30, 72
SLIP, STOP_SLIP, TAKER, MAKER = 0.003, 0.01, 0.0005, 0.0002
MIN_MS = 60_000


class PaperLongs:
    def __init__(self, client, store, notify=None, clock=None):
        self.client = client
        self.db = store.db
        self.notify = notify or (lambda msg, **kw: log.info(msg))
        self.clock = clock
        self.db.execute("""CREATE TABLE IF NOT EXISTS paper_longs (
            symbol TEXT, day INTEGER, entry_ms INTEGER, entry REAL, tp REAL, sl REAL, deadline_ms INTEGER,
            checked_ms INTEGER, status TEXT, exit REAL, exit_ms INTEGER, why TEXT, ret REAL,
            PRIMARY KEY (symbol, day))""")
        self.db.commit()
        self._last_tick = 0

    def open(self, cands, day: int, majors: float) -> list[str]:
        if majors >= MAJORS_BELOW or not cands:
            return []
        entry_ms = (day + 1) * DAY_MS
        notes = []
        held = {r[0] for r in self.db.execute("SELECT symbol FROM paper_longs WHERE status='open'")}
        for c in cands:
            if c.symbol in held:
                continue
            try:
                rows = self.client.klines(c.symbol, "1h", start_ms=entry_ms, limit=1)
                if not rows or int(rows[0][0]) != entry_ms:
                    continue
                entry = float(rows[0][1]) * (1 + SLIP)
            except Exception as exc:
                log.warning("paper long %s: no price (%s)", c.symbol, exc)
                continue
            self.db.execute("INSERT OR IGNORE INTO paper_longs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (c.symbol, day, entry_ms, entry, entry * (1 + TP), entry * (1 - STOP),
                             entry_ms + HOLD_H * HOUR_MS, entry_ms, "open", None, None, None, None))
            notes.append(f"🟢 [paper long] {c.symbol} (+{c.change:.0%} on a {majors:+.1%} majors day) bought @ "
                         f"{entry:.6g}, TP {entry * (1 + TP):.6g} (+8%), stop {entry * (1 - STOP):.6g} (−30%), 3 days")
        self.db.commit()
        for n in notes:
            self.notify(n)
        return notes

    def tick(self) -> None:
        now = self.clock()
        if now - self._last_tick < MIN_MS:
            return
        self._last_tick = now
        for sym, day, entry, tp, sl, deadline, checked in self.db.execute(
                "SELECT symbol, day, entry, tp, sl, deadline_ms, checked_ms FROM paper_longs WHERE status='open'").fetchall():
            try:
                self._check(sym, day, entry, tp, sl, deadline, checked, now)
            except Exception as exc:
                log.warning("paper long %s check failed: %s", sym, exc)

    def _check(self, sym, day, entry, tp, sl, deadline, checked, now):
        rows = self.client.klines(sym, "1m", start_ms=checked, end_ms=min(now, deadline) - 1, limit=1000)
        done = None
        last = checked
        for r in rows:
            t, o, h, l, c = int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])
            if t + MIN_MS > now:
                break  # the current minute is not finished
            last = t + MIN_MS
            if l <= sl:
                done = ("stop", min(sl, o) * (1 - STOP_SLIP), t, TAKER)
                break
            if h >= tp:
                done = ("take-profit", tp, t, MAKER)
                break
            if t + MIN_MS >= deadline:
                done = ("3 days", c * (1 - SLIP), t, TAKER)
                break
        if done is None and now >= deadline and rows and len(rows) < 1000:  # every bar up to the deadline is in
            done = ("3 days", float(rows[-1][4]) * (1 - SLIP), int(rows[-1][0]), TAKER)
        if done is None:
            self.db.execute("UPDATE paper_longs SET checked_ms=? WHERE symbol=? AND day=?", (last, sym, day))
            self.db.commit()
            return
        why, px, t, fee = done
        ret = (px / entry - 1) - TAKER - fee * px / entry
        self.db.execute("UPDATE paper_longs SET status='closed', exit=?, exit_ms=?, why=?, ret=?, checked_ms=? "
                        "WHERE symbol=? AND day=?", (px, t, why, ret, t, sym, day))
        self.db.commit()
        self.notify(f"{'✅' if ret > 0 else '❌'} [paper long] {sym} closed by {why}: {entry:.6g} → {px:.6g}, "
                    f"{ret:+.1%} (12% of $10k would be ${1200 * ret:+,.0f})")

    def summary(self) -> str:
        rows = self.db.execute("SELECT ret FROM paper_longs WHERE status='closed'").fetchall()
        open_n = self.db.execute("SELECT COUNT(*) FROM paper_longs WHERE status='open'").fetchone()[0]
        if not rows and not open_n:
            return "paper longs: none yet (they only trigger on days the majors fall more than 3%)"
        r = [x[0] for x in rows]
        if not r:
            return f"paper longs: {open_n} open, none closed yet"
        return (f"paper longs: {len(r)} closed, win {sum(x > 0 for x in r) / len(r):.0%}, per trade "
                f"{sum(r) / len(r):+.2%}, worst {min(r):+.1%}, {open_n} open (backtest: ~80% win, +2-4% per trade)")

"""SQLite journal: every order, fill, event and periodic account snapshot.

The report and the daily cost budget are computed from here, so a restart loses
nothing. Values are stored as TEXT decimals to keep them exact.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from decimal import Decimal
from pathlib import Path

from .models import AccountState, Fill, OrderState
from .util import D, ZERO

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, level TEXT NOT NULL, kind TEXT NOT NULL,
    pair TEXT, venue TEXT, message TEXT, data TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, venue TEXT, symbol TEXT, pair TEXT, purpose TEXT,
    order_id TEXT, client_id TEXT, side TEXT, tif TEXT, price TEXT, size TEXT, reduce_only INTEGER,
    status TEXT, reject_reason TEXT
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, venue TEXT, symbol TEXT, pair TEXT, purpose TEXT,
    order_id TEXT, side TEXT, size TEXT, price TEXT, notional TEXT, fee TEXT, liquidity TEXT,
    mid TEXT, slippage_usd TEXT, outside_rth INTEGER, hedge_latency_ms INTEGER
);
CREATE TABLE IF NOT EXISTS venue_fills (
    fill_id TEXT, venue TEXT, ts_ms INTEGER, symbol TEXT, order_id TEXT, side TEXT, size TEXT, price TEXT,
    fee TEXT, liquidity TEXT, PRIMARY KEY (venue, fill_id)
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, venue TEXT, equity TEXT, free_collateral TEXT,
    maintenance TEXT, gross_notional TEXT, positions TEXT
);
CREATE TABLE IF NOT EXISTS pair_state (
    pair TEXT PRIMARY KEY, data TEXT NOT NULL, updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS fills_ts ON fills(ts);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS snapshots_ts ON snapshots(ts);
"""


def _s(value) -> str | None:
    return None if value is None else str(value)


class Journal:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def _exec(self, sql: str, params=()):
        with self.lock:
            return self.db.execute(sql, params)

    def close(self) -> None:
        self.db.close()

    # ---------------------------------------------------------------- writes
    def event(self, level: str, kind: str, message: str, *, pair: str | None = None, venue: str | None = None,
              data: dict | None = None) -> None:
        self._exec("INSERT INTO events(ts, level, kind, pair, venue, message, data) VALUES (?,?,?,?,?,?,?)",
                   (time.time(), level, kind, pair, venue, message, json.dumps(data, default=str) if data else None))

    def order(self, venue: str, pair: str, purpose: str, order: OrderState) -> None:
        self._exec(
            "INSERT INTO orders(ts, venue, symbol, pair, purpose, order_id, client_id, side, tif, price, size,"
            " reduce_only, status, reject_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), venue, order.symbol, pair, purpose, order.order_id, order.client_id, order.side.value,
             order.tif.value, _s(order.price), _s(order.size), int(order.reduce_only), order.status.value,
             order.reject_reason),
        )

    def fill(self, *, venue: str, symbol: str, pair: str, purpose: str, order_id: str, side: str, size: Decimal,
             price: Decimal, fee: Decimal, liquidity: str, mid: Decimal | None, outside_rth: bool,
             hedge_latency_ms: int | None = None) -> None:
        notional = size * price
        slippage = None
        if mid is not None and mid > 0:
            # positive = paid versus mid (buy above / sell below)
            slippage = (price - mid) * size * (1 if side == "BUY" else -1)
        self._exec(
            "INSERT INTO fills(ts, venue, symbol, pair, purpose, order_id, side, size, price, notional, fee,"
            " liquidity, mid, slippage_usd, outside_rth, hedge_latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), venue, symbol, pair, purpose, order_id, side, _s(size), _s(price), _s(notional), _s(fee),
             liquidity, _s(mid), _s(slippage), int(outside_rth), hedge_latency_ms),
        )

    def venue_fills(self, fills: list[Fill]) -> int:
        added = 0
        for f in fills:
            if not f.fill_id:
                continue
            cur = self._exec(
                "INSERT OR IGNORE INTO venue_fills(fill_id, venue, ts_ms, symbol, order_id, side, size, price, fee,"
                " liquidity) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f.fill_id, f.venue, f.ts_ms, f.symbol, f.order_id, f.side.value, _s(f.size), _s(f.price), _s(f.fee),
                 f.liquidity),
            )
            added += cur.rowcount
        return added

    def snapshot(self, account: AccountState) -> None:
        positions = {s: {"size": str(p.size), "entry": str(p.entry_price), "mark": str(p.mark_price),
                         "liq": _s(p.liquidation_price), "mode": p.margin_mode}
                     for s, p in account.positions.items()}
        self._exec(
            "INSERT INTO snapshots(ts, venue, equity, free_collateral, maintenance, gross_notional, positions)"
            " VALUES (?,?,?,?,?,?,?)",
            (account.ts, account.venue, _s(account.equity), _s(account.free_collateral),
             _s(account.maintenance_margin), _s(account.gross_notional), json.dumps(positions)),
        )

    def save_pair_state(self, pair: str, data: dict) -> None:
        self._exec("INSERT INTO pair_state(pair, data, updated) VALUES (?,?,?) ON CONFLICT(pair) DO UPDATE SET"
                   " data=excluded.data, updated=excluded.updated", (pair, json.dumps(data, default=str), time.time()))

    # ----------------------------------------------------------------- reads
    def load_pair_state(self, pair: str) -> dict | None:
        row = self._exec("SELECT data FROM pair_state WHERE pair=?", (pair,)).fetchone()
        return json.loads(row[0]) if row else None

    def cost_since(self, ts: float) -> Decimal:
        """Fees plus taker slippage versus mid since ts (the rotation budget meter)."""
        total = ZERO
        for fee, slip in self._exec("SELECT fee, slippage_usd FROM fills WHERE ts >= ?", (ts,)).fetchall():
            total += D(fee or 0) + max(ZERO, D(slip or 0))
        return total

    def fills_between(self, start: float, end: float) -> list[dict]:
        cur = self._exec("SELECT * FROM fills WHERE ts >= ? AND ts < ? ORDER BY ts", (start, end))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def events_between(self, start: float, end: float) -> list[dict]:
        cur = self._exec("SELECT * FROM events WHERE ts >= ? AND ts < ? ORDER BY ts", (start, end))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def snapshots_between(self, start: float, end: float) -> list[dict]:
        cur = self._exec("SELECT * FROM snapshots WHERE ts >= ? AND ts < ? ORDER BY ts", (start, end))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def venue_fills_between(self, start_ms: int, end_ms: int) -> list[dict]:
        cur = self._exec("SELECT * FROM venue_fills WHERE ts_ms >= ? AND ts_ms < ? ORDER BY ts_ms", (start_ms, end_ms))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def first_ts(self) -> float | None:
        row = self._exec("SELECT MIN(ts) FROM snapshots").fetchone()
        return row[0] if row and row[0] else None

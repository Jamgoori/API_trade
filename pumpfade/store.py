"""SQLite persistence for the paper bot: legs, books, bans, signal log and small key/value state."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path


@dataclass
class Leg:
    """Half (or all) of one short. Status: pending (limit resting) → open → closed, or pending → cancelled."""

    book: str
    symbol: str
    signal_day: int
    part: str                  # "A" market at 00:00 UTC, "B" limit then market at 01:00 UTC
    kind: str                  # "market" | "limit" — how it was (or will be) entered
    status: str
    margin: float              # margin of the whole signal (both legs), for per-signal returns
    notional: float            # target notional of this leg
    ref_px: float              # 00:00 UTC open of the hour (what the backtest enters at)
    hold_hours: int
    sleeve: str = "main"       # "main" (daily +20% rule) or "listing" (listing pump)
    limit_px: float | None = None
    limit_until_ms: int | None = None
    qty: float = 0.0
    entry_px: float | None = None
    entry_ms: int | None = None
    entry_slip: float | None = None
    fill_ratio: float | None = None
    tp_px: float | None = None
    stop_px: float | None = None
    deadline_ms: int | None = None
    exit_px: float | None = None
    exit_ms: int | None = None
    exit_reason: str | None = None
    exit_slip: float | None = None
    fees: float = 0.0
    funding: float = 0.0
    pnl: float | None = None
    checked_ms: int | None = None      # last 1m candle processed (open time)
    funding_ms: int | None = None      # last funding settlement applied
    id: int | None = None


LEG_FIELDS = [f.name for f in fields(Leg)]


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        cols = ", ".join(f"{n} {'INTEGER PRIMARY KEY AUTOINCREMENT' if n == 'id' else ''}" for n in LEG_FIELDS)
        self.db.executescript(f"""
            CREATE TABLE IF NOT EXISTS legs ({cols});
            CREATE TABLE IF NOT EXISTS books (name TEXT PRIMARY KEY, equity REAL, peak REAL);
            CREATE TABLE IF NOT EXISTS equity_log (ts INTEGER, book TEXT, equity REAL);
            CREATE TABLE IF NOT EXISTS bans (book TEXT, symbol TEXT, day INTEGER, PRIMARY KEY (book, symbol));
            CREATE TABLE IF NOT EXISTS signals (day INTEGER, symbol TEXT, data TEXT, scanned_ms INTEGER,
                                                PRIMARY KEY (day, symbol));
            CREATE TABLE IF NOT EXISTS skips (day INTEGER, book TEXT, symbol TEXT, reason TEXT);
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS listings (venue TEXT, base TEXT, perp TEXT, detected_ms INTEGER,
                                                 start_ms INTEGER, entry_ms INTEGER, status TEXT, reason TEXT,
                                                 PRIMARY KEY (venue, base));
        """)
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(legs)").fetchall()}
        for name in LEG_FIELDS:
            if name not in have:  # databases created by an older version
                self.db.execute(f"ALTER TABLE legs ADD COLUMN {name}")
        self.db.commit()

    # key/value
    def get(self, key: str, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return json.loads(row["v"]) if row else default

    def put(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv (k, v) VALUES (?, ?)", (key, json.dumps(value)))
        self.db.commit()

    # books
    def book_equity(self, name: str, start: float) -> float:
        row = self.db.execute("SELECT equity FROM books WHERE name=?", (name,)).fetchone()
        if row is None:
            self.db.execute("INSERT INTO books (name, equity, peak) VALUES (?, ?, ?)", (name, start, start))
            self.db.commit()
            return start
        return row["equity"]

    def add_pnl(self, name: str, pnl: float, ts_ms: int) -> float:
        row = self.db.execute("SELECT equity, peak FROM books WHERE name=?", (name,)).fetchone()
        eq = row["equity"] + pnl
        self.db.execute("UPDATE books SET equity=?, peak=? WHERE name=?", (eq, max(row["peak"], eq), name))
        self.db.execute("INSERT INTO equity_log (ts, book, equity) VALUES (?, ?, ?)", (ts_ms, name, eq))
        self.db.commit()
        return eq

    def books(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM books ORDER BY name").fetchall()

    # legs
    def save_leg(self, leg: Leg) -> Leg:
        d = asdict(leg)
        if leg.id is None:
            d.pop("id")
            cur = self.db.execute(f"INSERT INTO legs ({', '.join(d)}) VALUES ({', '.join('?' * len(d))})",
                                  list(d.values()))
            leg.id = cur.lastrowid
        else:
            sets = ", ".join(f"{k}=?" for k in d if k != "id")
            self.db.execute(f"UPDATE legs SET {sets} WHERE id=?", [v for k, v in d.items() if k != "id"] + [leg.id])
        self.db.commit()
        return leg

    def legs(self, where: str = "1=1", params: tuple = ()) -> list[Leg]:
        rows = self.db.execute(f"SELECT * FROM legs WHERE {where} ORDER BY id", params).fetchall()
        return [Leg(**{k: r[k] for k in LEG_FIELDS}) for r in rows]

    def active_legs(self) -> list[Leg]:
        return self.legs("status IN ('pending', 'open')")

    # bans, signals, skips
    def is_banned(self, book: str, symbol: str) -> bool:
        return self.db.execute("SELECT 1 FROM bans WHERE book=? AND symbol=?", (book, symbol)).fetchone() is not None

    def ban(self, book: str, symbol: str, day: int) -> None:
        self.db.execute("INSERT OR IGNORE INTO bans (book, symbol, day) VALUES (?, ?, ?)", (book, symbol, day))
        self.db.commit()

    def log_signal(self, day: int, symbol: str, data: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO signals (day, symbol, data, scanned_ms) VALUES (?, ?, ?, ?)",
                        (day, symbol, json.dumps(data), int(time.time() * 1000)))
        self.db.commit()

    def signals(self, since_day: int = 0) -> list[dict]:
        rows = self.db.execute("SELECT data FROM signals WHERE day>=? ORDER BY day, symbol", (since_day,)).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def log_skip(self, day: int, book: str, symbol: str, reason: str) -> None:
        self.db.execute("INSERT INTO skips (day, book, symbol, reason) VALUES (?, ?, ?, ?)", (day, book, symbol, reason))
        self.db.commit()

    def listing(self, venue: str, base: str) -> dict | None:
        row = self.db.execute("SELECT * FROM listings WHERE venue=? AND base=?", (venue, base)).fetchone()
        return dict(row) if row else None

    def save_listing(self, row: dict) -> None:
        cols = ["venue", "base", "perp", "detected_ms", "start_ms", "entry_ms", "status", "reason"]
        self.db.execute(f"INSERT OR REPLACE INTO listings ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        [row.get(c) for c in cols])
        self.db.commit()

    def listings(self, status: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM listings", ()
        if status:
            q, args = q + " WHERE status=?", (status,)
        return [dict(r) for r in self.db.execute(q + " ORDER BY detected_ms", args).fetchall()]

    def equity_log(self, book: str) -> list[tuple[int, float]]:
        return [(r["ts"], r["equity"]) for r in
                self.db.execute("SELECT ts, equity FROM equity_log WHERE book=? ORDER BY ts", (book,)).fetchall()]

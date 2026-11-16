"""Live engine against a simulated Binance futures account (orders, algo orders, positions, fills)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from pumpfade.config import Settings
from pumpfade.live import LiveEngine
from pumpfade.signals import DAY_MS, HOUR_MS, Candidate
from pumpfade.store import Store
from pumpfade.trader import Filters

MIN = 60_000
DAY = 20_000
ENTRY = (DAY + 1) * DAY_MS
SYM = "TGTUSDT"


class FakeExchange:
    def __init__(self, equity=10_000.0):
        self.cash = equity
        self.px = 1.0
        self.orders: dict[str, dict] = {}
        self.algos: dict[str, dict] = {}
        self.pos = 0.0          # negative = short
        self.entry = 0.0
        self.trades: list[dict] = []
        self.now = ENTRY + MIN
        self.fail_stop = False
        self.extra_positions: dict[str, float] = {}
        self.prepared = []

    # --- helpers for tests
    def _fill(self, side, qty, price, maker=False):
        fee = qty * price * (0.0002 if maker else 0.0005)
        realized = 0.0
        if side == "SELL":
            self.entry = (self.entry * -self.pos + price * qty) / (-self.pos + qty)
            self.pos -= qty
        else:
            realized = (self.entry - price) * qty
            self.pos += qty
            if abs(self.pos) < 1e-12:
                self.pos = 0.0
        self.cash += realized - fee
        self.trades.append({"side": side, "qty": str(qty), "price": str(price), "realizedPnl": str(realized),
                            "commission": str(fee), "commissionAsset": "USDT", "time": self.now})

    def set_price(self, px):
        self.px = px
        for o in self.orders.values():
            if o["status"] in ("NEW", "PARTIALLY_FILLED") and o["side"] == "SELL" and o["tif"] == "GTC" and px >= o["price"]:
                q = o["qty"] - o["executed"]
                self._fill("SELL", q, o["price"], maker=True)
                o["avg"] = o["price"]
                o["executed"] = o["qty"]
                o["status"] = "FILLED"
        for a in list(self.algos.values()):
            if a["status"] != "NEW" or self.pos == 0:
                continue
            hit = (a["type"] == "STOP_MARKET" and px >= a["trigger"]) or (a["type"] == "TAKE_PROFIT_MARKET" and px <= a["trigger"])
            if hit:
                self._fill("BUY", -self.pos, px)
                a["status"] = "FINISHED"

    def partial_fill(self, cid, qty):
        o = self.orders[cid]
        self._fill("SELL", qty, o["price"], maker=True)
        o["executed"] += qty
        o["avg"] = o["price"]
        o["status"] = "PARTIALLY_FILLED"

    # --- trader interface
    def account(self):
        unreal = (self.entry - self.px) * -self.pos if self.pos else 0.0
        positions = [{"symbol": SYM, "positionAmt": str(self.pos)}] if self.pos else []
        positions += [{"symbol": s, "positionAmt": str(a)} for s, a in self.extra_positions.items()]
        return {"totalMarginBalance": str(self.cash + unreal), "positions": positions}

    def filters(self, symbol):
        return Filters(Decimal("0.0001"), Decimal("1"), Decimal("1"), Decimal("5"))

    def prepare_symbol(self, symbol, leverage=1):
        self.prepared.append((symbol, leverage))

    def book_top(self, symbol):
        return self.px * 0.999, self.px * 1.001

    def new_order(self, **p):
        cid = p["newClientOrderId"]
        qty, typ = float(p["quantity"]), p["type"]
        o = {"side": p["side"], "qty": qty, "price": float(p.get("price") or 0), "tif": p.get("timeInForce"),
             "executed": 0.0, "avg": 0.0, "status": "NEW"}
        self.orders[cid] = o
        bid, ask = self.book_top(SYM)
        if typ == "LIMIT" and p["timeInForce"] == "IOC":
            if bid >= o["price"]:
                self._fill("SELL", qty, bid)
                o.update(executed=qty, avg=bid, status="FILLED")
            else:
                o["status"] = "EXPIRED"
        elif typ == "MARKET":
            q = min(qty, -self.pos) if p.get("reduceOnly") == "true" else qty
            self._fill(p["side"], q, ask)
            o.update(executed=q, avg=ask, status="FILLED")
        elif p["side"] == "SELL" and self.px >= o["price"]:
            self.set_price(self.px)
        return self._view(o)

    def _view(self, o):
        return {"status": o["status"], "executedQty": str(o["executed"]), "avgPrice": str(o["avg"])}

    def query_order(self, symbol, cid):
        o = self.orders.get(cid)
        return self._view(o) if o else None

    def cancel_order(self, symbol, cid):
        o = self.orders.get(cid)
        if o and o["status"] in ("NEW", "PARTIALLY_FILLED"):
            o["status"] = "CANCELED"
        return self._view(o) if o else None

    def cancel_all_orders(self, symbol):
        for o in self.orders.values():
            if o["status"] in ("NEW", "PARTIALLY_FILLED"):
                o["status"] = "CANCELED"

    def new_algo(self, **p):
        if p["type"] == "STOP_MARKET" and self.fail_stop:
            raise RuntimeError("rejected")
        self.algos[p["clientAlgoId"]] = {"type": p["type"], "trigger": float(p["triggerPrice"]), "status": "NEW"}
        return {"algoStatus": "NEW"}

    def query_algo(self, cid):
        a = self.algos.get(cid)
        return {"algoStatus": a["status"]} if a else None

    def cancel_algo(self, cid):
        if cid in self.algos and self.algos[cid]["status"] == "NEW":
            self.algos[cid]["status"] = "CANCELED"

    def cancel_all_algos(self, symbol):
        for a in self.algos.values():
            if a["status"] == "NEW":
                a["status"] = "CANCELED"

    def user_trades(self, symbol, start_ms):
        return [t for t in self.trades if t["time"] >= start_ms]

    def funding_income(self, symbol, start_ms):
        return 1.25


class FakePublic:
    def klines(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
        return [[ENTRY, "1.0", "1.0", "1.0", "1.0", "0", ENTRY + HOUR_MS - 1, "0"]]


def cand():
    return Candidate(SYM, DAY, 0.25, 150, 0.05, 0.0, ratio=2.0, funding=0.0, funding_interval_h=4, passed=True)


@pytest.fixture
def env(tmp_path):
    ex = FakeExchange()
    s = Settings(db_path=tmp_path / "live.sqlite")
    s.live_margin_fraction = 0.12
    store = Store(s.db_path)
    msgs = []
    eng = LiveEngine(ex, FakePublic(), store, s, notify=lambda m, **k: msgs.append(m), clock=lambda: ex.now)
    return ex, store, eng, msgs


def settle(eng, ex):
    """Two ticks 30s apart: a flat position only counts as closed once it reads zero twice."""
    eng.tick()
    ex.now += 30_000
    eng.tick()


def open_algos(ex):
    return sorted(a["type"] for a in ex.algos.values() if a["status"] == "NEW")


def test_limit_fill_is_protected_then_takes_profit(env):
    ex, store, eng, msgs = env
    eng.open_signals([cand()], DAY)
    p = eng.book.all()[0]
    assert ex.prepared == [(SYM, 1)]
    assert p.limit_px == pytest.approx(1.005) and p.qty_target == 1194  # 12% of $10k at 1.005, whole coins
    ex.now += 10 * MIN
    ex.set_price(1.01)               # trades through the limit
    eng.tick()
    p = eng.book.all()[0]
    assert p.status == "open" and p.entry_avg == pytest.approx(1.005)
    assert open_algos(ex) == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]
    assert p.sl_px == pytest.approx(1.1558, abs=1e-4) and p.tp_px == pytest.approx(0.8542, abs=1e-4)
    ex.now += 6 * HOUR_MS
    ex.set_price(0.85)               # take-profit triggers on the exchange
    settle(eng, ex)
    p = eng.book.all()[0]
    assert p.status == "closed" and p.exit_reason == "take-profit"
    assert p.pnl == pytest.approx(1194 * (1.005 - 0.85) - p.fees + 1.25)
    assert open_algos(ex) == []      # the leftover stop was cancelled
    assert not store.is_banned("live", SYM)


def test_no_limit_fill_falls_back_to_ioc_at_ten(env):
    ex, store, eng, msgs = env
    eng.open_signals([cand()], DAY)
    ex.set_price(0.98)
    ex.now = ENTRY + HOUR_MS + 30_000
    eng.tick()
    p = eng.book.all()[0]
    assert ex.orders[p.entry_cid]["status"] == "CANCELED"
    assert p.status == "open" and p.entry_avg == pytest.approx(0.98 * 0.999)
    assert p.qty == p.qty_target and open_algos(ex) == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]


def test_stop_out_drops_the_coin(env):
    ex, store, eng, msgs = env
    eng.open_signals([cand()], DAY)
    ex.set_price(1.01)
    eng.tick()
    ex.set_price(1.2)
    settle(eng, ex)
    p = eng.book.all()[0]
    assert p.exit_reason == "stop" and p.pnl < 0
    assert store.is_banned("live", SYM)
    eng.open_signals([cand()], DAY + 3)  # the same coin pumps again later
    assert len(eng.book.all()) == 1


def test_time_exit_after_48_hours_and_restart_safety(env, tmp_path):
    ex, store, eng, msgs = env
    eng.open_signals([cand()], DAY)
    ex.set_price(1.01)
    eng.tick()
    deadline = eng.book.all()[0].deadline_ms
    # a fresh process with the same database carries on
    eng2 = LiveEngine(ex, FakePublic(), Store(tmp_path / "live.sqlite"), eng.settings, notify=lambda *a, **k: None,
                      clock=lambda: ex.now)
    ex.now = deadline + MIN
    eng2.tick()
    assert eng2.book.all()[0].status == "closing" and ex.pos == 0
    settle(eng2, ex)
    p = eng2.book.all()[0]
    assert p.status == "closed" and p.exit_reason == "time" and open_algos(ex) == []


def test_partial_fill_then_stop_inside_the_hour_does_not_reenter(env):
    ex, store, eng, msgs = env
    eng.open_signals([cand()], DAY)
    p = eng.book.all()[0]
    ex.partial_fill(p.entry_cid, 300)
    eng.tick()
    assert eng.book.all()[0].qty == 300 and open_algos(ex) == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]
    ex.px = 1.2                       # spike: only the stop fires, the resting sell is still below this price...
    for a in ex.algos.values():
        if a["type"] == "STOP_MARKET" and a["status"] == "NEW":
            ex._fill("BUY", -ex.pos, 1.2)
            a["status"] = "FINISHED"
    settle(eng, ex)
    p = eng.book.all()[0]
    assert p.status == "closed" and p.exit_reason == "stop"
    assert ex.orders[p.entry_cid]["status"] == "CANCELED" and ex.pos == 0
    ex.now = ENTRY + HOUR_MS + MIN
    eng.tick()
    assert ex.pos == 0                # no 10:00 IOC after the position is gone


def test_failed_stop_closes_the_position_at_once(env):
    ex, store, eng, msgs = env
    ex.fail_stop = True
    eng.open_signals([cand()], DAY)
    ex.set_price(1.01)
    eng.tick()
    assert ex.pos == 0
    settle(eng, ex)
    p = eng.book.all()[0]
    assert p.status == "closed" and p.exit_reason == "no stop"
    assert any("STOP could not be placed" in m for m in msgs)


def test_drawdown_cuts_size_then_halts(env):
    ex, store, eng, msgs = env
    assert eng.risk_state()["fraction"] == pytest.approx(0.12)
    ex.cash = 7_400                   # −26% from the 10k peak
    eng._account_cache = None
    rs = eng.risk_state()
    assert rs["fraction"] == pytest.approx(0.08) and not rs["halted"]
    ex.cash = 8_000                   # still below the peak: stays reduced
    eng._account_cache = None
    assert eng.risk_state()["fraction"] == pytest.approx(0.08)
    ex.cash = 6_400                   # −36%
    eng._account_cache = None
    assert eng.risk_state()["halted"]
    notes = eng.open_signals([cand()], DAY)
    assert eng.book.all() == [] and "halted" in notes[0]


def test_listing_entry_and_positions_the_bot_did_not_open(env):
    ex, store, eng, msgs = env
    ex.extra_positions = {"OTHERUSDT": -5.0}
    notes = eng.open_listing(SYM, "upbit", ENTRY + 3 * HOUR_MS)
    p = eng.book.all()[0]
    assert p.sleeve == "listing" and p.status == "open" and "listing short" in notes[0]
    eng.tick()
    assert any("did not open" in m and "OTHERUSDT" in m for m in msgs)


def test_a_single_stale_zero_read_does_not_close_the_trade(env):
    ex, store, eng, msgs = env
    eng.open_signals([cand()], DAY)
    ex.set_price(1.01)
    eng.tick()
    real = ex.account
    ex.account = lambda: {**real(), "positions": []}  # the account endpoint lags once
    eng.tick()
    ex.account = real
    ex.now += 30_000
    eng.tick()
    p = eng.book.all()[0]
    assert p.status == "open" and p.flat_ms is None
    assert open_algos(ex) == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]


def test_restart_long_after_ten_skips_the_fallback(env):
    ex, store, eng, msgs = env
    eng.open_signals([cand()], DAY)
    ex.set_price(0.98)
    ex.now = ENTRY + 6 * HOUR_MS      # the bot was down from 09:05 to 15:00 KST
    eng.tick()
    p = eng.book.all()[0]
    assert p.status == "failed" and ex.pos == 0 and "not running" in p.note
    assert ex.orders[p.entry_cid]["status"] == "CANCELED"


def test_flatten_closes_everything_and_stops_new_entries(env):
    ex, store, eng, msgs = env
    eng.open_signals([cand()], DAY)
    ex.set_price(1.01)
    eng.tick()
    eng.flatten()
    assert ex.pos == 0
    settle(eng, ex)
    assert eng.book.all()[0].exit_reason == "manual flatten"
    notes = eng.open_signals([cand()], DAY + 5)
    assert "halted" in notes[0]

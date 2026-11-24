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
        self.query_has_avg = True
        # what the selftest needs besides the bot's calls
        self.base_url, self.offset_ms, self.spot_http = "fake", 0, None

    # --- helpers for tests
    def sync_time(self):
        pass

    def dual_side_position(self):
        return False

    def positions(self):
        return {SYM: self.pos} if self.pos else {}

    def order_trades(self, symbol, order_id):
        return [t for t in self.trades if t.get("orderId") == order_id]

    def _fill(self, side, qty, price, maker=False, oid=None):
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
                            "commission": str(fee), "commissionAsset": "USDT", "time": self.now, "orderId": oid})

    def set_price(self, px):
        self.px = px
        for o in self.orders.values():
            if o["status"] in ("NEW", "PARTIALLY_FILLED") and o["side"] == "SELL" and o["tif"] == "GTC" and px >= o["price"]:
                q = o["qty"] - o["executed"]
                self._fill("SELL", q, o["price"], maker=True, oid=o["orderId"])
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
        self._fill("SELL", qty, o["price"], maker=True, oid=o["orderId"])
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
             "executed": 0.0, "avg": 0.0, "status": "NEW", "orderId": len(self.orders) + 1, "cid": cid}
        self.orders[cid] = o
        bid, ask = self.book_top(SYM)
        if typ == "LIMIT" and p["timeInForce"] == "IOC":
            if bid >= o["price"]:
                self._fill("SELL", qty, bid, oid=o["orderId"])
                o.update(executed=qty, avg=bid, status="FILLED")
            else:
                o["status"] = "EXPIRED"
        elif typ == "MARKET":
            q = min(qty, -self.pos) if p.get("reduceOnly") == "true" else qty
            self._fill(p["side"], q, ask, oid=o["orderId"])
            o.update(executed=q, avg=ask, status="FILLED")
        elif p["side"] == "SELL" and self.px >= o["price"]:
            self.set_price(self.px)
        return self._view(o, avg=False)  # like Binance today: the order response carries no avgPrice

    def _view(self, o, avg=True):
        v = {"status": o["status"], "executedQty": str(o["executed"]), "orderId": o["orderId"],
             "clientOrderId": o["cid"]}
        if avg:
            v["avgPrice"] = str(o["avg"])
        return v

    def query_order(self, symbol, cid):
        o = self.orders.get(cid)
        return self._view(o, avg=self.query_has_avg) if o else None

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
        self.algos[p["clientAlgoId"]] = {"type": p["type"], "trigger": float(p["triggerPrice"]), "status": "NEW",
                                         "algoId": len(self.algos) + 1}
        return {"algoStatus": "NEW", "algoId": len(self.algos)}

    def query_algo(self, cid):
        a = self.algos.get(cid)
        return {"algoStatus": a["status"]} if a else None

    def cancel_algo(self, cid=None, algo_id=None):
        for k, a in self.algos.items():
            if (k == cid or a["algoId"] == algo_id) and a["status"] == "NEW":
                a["status"] = "CANCELED"

    def open_algos(self, symbol):
        return [{"algoId": a["algoId"], "clientAlgoId": k, "algoStatus": "NEW", "symbol": SYM}
                for k, a in self.algos.items() if a["status"] == "NEW"]

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
    return Candidate(SYM, DAY, 0.25, 150, 0.05, 0.0, ratio=2.5, funding=0.0, funding_interval_h=4, passed=True)


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


def test_a_coin_shorted_in_the_last_three_days_is_not_shorted_again(env):
    ex, store, eng, msgs = env
    ex.now = ENTRY + 3 * HOUR_MS
    eng.open_listing(SYM, "upbit", ex.now)           # first KRW listing
    ex.set_price(1.2)                                # stopped out within the hour
    settle(eng, ex)
    assert eng.book.all()[0].exit_reason == "stop"
    ex.set_price(1.0)
    ex.now += 3 * HOUR_MS
    notes = eng.open_listing(SYM, "bithumb", ex.now)  # the same coin lists on a second exchange
    assert "last 72h" in notes[0] and len(eng.book.all()) == 1
    eng.open_signals([cand()], DAY + 1)              # nor the main rule the next morning
    assert len(eng.book.all()) == 1
    ex.now += 72 * HOUR_MS
    eng.open_listing(SYM, "binance_spot", ex.now)    # three days later it may trade again
    assert len(eng.book.all()) == 2


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


def test_network_errors_do_not_leak_the_signed_query(caplog):
    from pumpfade.trader import BinanceTrader

    t = BinanceTrader("k" * 64, "s" * 64, "https://127.0.0.1:9")  # nothing listens there
    t.http.timeout = 0.5
    with pytest.raises(Exception) as ei:
        t.signed("POST", "/fapi/v1/order", {"symbol": "TGTUSDT", "side": "SELL", "quantity": 5})
    msg = str(ei.value)
    assert "signature" not in msg and "timestamp" not in msg and "TGTUSDT" not in msg
    assert ei.value.__cause__ is None and ei.value.__suppress_context__
    assert "s" * 64 not in msg and "k" * 64 not in msg


def test_telegram_failures_do_not_log_the_bot_token(monkeypatch, caplog):
    import requests

    from hedgebot.notifier import Notifier

    token = "123456789:AAH" + "x" * 32
    n = Notifier(token, "42")

    def boom(url, **kw):
        raise requests.ConnectionError(f"Max retries exceeded with url: {url.split('.org')[1]}")

    monkeypatch.setattr(requests, "post", boom)
    n._post("hello")
    assert "telegram send failed" in caplog.text and token not in caplog.text


def test_settings_repr_hides_secrets():
    s = Settings(api_key="KEY123", api_secret="SECRET456", telegram_token="TOK789")
    assert not any(x in repr(s) for x in ("KEY123", "SECRET456", "TOK789"))


class DiagTrader:
    spot_http = object()

    def __init__(self, perms, spot=0.0):
        self.perms, self.spot = perms, spot

    def api_restrictions(self):
        return self.perms

    def spot_balance(self, asset):
        return self.spot


def test_selftest_diagnosis_names_missing_futures_permission_and_empty_wallet():
    from pumpfade.selftest import diagnose

    lines = []
    ok = diagnose(DiagTrader({"enableReading": True, "enableFutures": False, "ipRestrict": True}, spot=14_932.8), 0.0,
                  lines.append)
    text = "\n".join(lines)
    assert not ok and "Enable Futures is OFF" in text and "futures wallet of this key's account is empty" in text
    assert "14,932.80" in text
    lines.clear()
    ok = diagnose(DiagTrader({"enableReading": True, "enableFutures": True, "enableWithdrawals": True,
                              "ipRestrict": False}), 3_000.0, lines.append)
    assert not ok and any("withdrawals are ON" in x for x in lines) and any("not IP-restricted" in x for x in lines)
    lines.clear()
    assert diagnose(DiagTrader({"enableReading": True, "enableFutures": True, "ipRestrict": True}), 3_000.0, lines.append)


def test_ioc_fill_price_comes_from_the_trades_when_no_response_carries_it(env):
    ex, store, eng, msgs = env
    ex.query_has_avg = False
    eng.open_signals([cand()], DAY)
    ex.set_price(0.98)
    ex.now = ENTRY + HOUR_MS + 30_000
    eng.tick()
    p = eng.book.all()[0]
    assert p.status == "open" and p.entry_avg == pytest.approx(0.98 * 0.999)
    assert p.sl_px == pytest.approx(0.98 * 0.999 * 1.15, abs=1e-4)


def test_selftest_fill_round_trip_and_leftover_handling():
    from pumpfade.selftest import run

    ex = FakeExchange()
    ex.query_has_avg = False
    lines = []
    assert run(ex, SYM, fill=True, out=lines.append), "\n".join(lines)
    assert ex.pos == 0 and any("fill price read back: 0.999" in x for x in lines)
    ex._fill("SELL", 102, 1.0)       # a short left behind by an earlier run
    lines.clear()
    assert not run(ex, SYM, fill=True, out=lines.append) and ex.pos == -102
    assert any("already open" in x for x in lines)
    lines.clear()
    assert run(ex, SYM, fill=True, out=lines.append, close_leftover=True) and ex.pos == 0


def test_selftest_never_leaves_a_position_when_a_step_blows_up():
    from pumpfade.selftest import run

    ex = FakeExchange()

    def broken(**p):
        raise RuntimeError("algo endpoint down")

    ex.new_algo = broken
    lines = []
    assert not run(ex, SYM, fill=True, out=lines.append)
    assert ex.pos == 0 and any("position after close: 0" in x for x in lines)


def test_leftover_conditional_orders_are_cleared_before_a_new_entry(env):
    ex, store, eng, msgs = env
    ex.algos["stale-T"] = {"type": "TAKE_PROFIT_MARKET", "trigger": 1.5, "status": "NEW", "algoId": 99}
    eng.open_signals([cand()], DAY)
    assert ex.algos["stale-T"]["status"] == "CANCELED"
    assert eng.book.all()[0].status == "entering"


def test_entry_is_skipped_when_old_conditional_orders_will_not_go_away(env):
    ex, store, eng, msgs = env
    ex.algos["stuck"] = {"type": "STOP_MARKET", "trigger": 1.5, "status": "NEW", "algoId": 7}
    ex.cancel_all_algos = lambda symbol: None
    ex.cancel_algo = lambda cid=None, algo_id=None: None
    notes = eng.open_signals([cand()], DAY)
    assert eng.book.all() == [] and "could not be removed" in notes[0]


def test_close_alerts_when_conditional_orders_survive(env):
    ex, store, eng, msgs = env
    eng.open_signals([cand()], DAY)
    ex.set_price(1.01)
    eng.tick()
    ex.cancel_all_algos = lambda symbol: None
    ex.cancel_algo = lambda cid=None, algo_id=None: None
    ex.set_price(0.85)                 # take-profit fires; the stop stays behind
    settle(eng, ex)
    assert eng.book.all()[0].status == "closed"
    assert any("may still be open" in m for m in msgs)


def test_a_second_live_process_on_the_same_database_is_refused(tmp_path):
    from pumpfade.cli import single_instance

    held = single_instance(tmp_path / "live.sqlite")
    with pytest.raises(SystemExit) as ei:
        single_instance(tmp_path / "live.sqlite")
    assert "already using" in str(ei.value)
    held.close()
    single_instance(tmp_path / "live.sqlite").close()  # free again once the first one exits


def test_telegram_setup_finds_the_chat_then_sends_a_test(monkeypatch):
    import requests

    from pumpfade.cli import telegram_check

    class R:
        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    token = "123:ABC" + "x" * 30
    monkeypatch.setattr(requests, "get", lambda url, **k: R({"ok": True, "result": [
        {"message": {"chat": {"id": 4242, "first_name": "me"}}}]}))
    sent = []
    monkeypatch.setattr(requests, "post", lambda url, json=None, **k: sent.append(json) or R({"ok": True}))
    lines = []
    assert telegram_check(token, None, lines.append) and "TELEGRAM_CHAT_ID=4242" in lines[0]
    assert telegram_check(token, "4242", lines.append) and sent[0]["chat_id"] == "4242"
    assert not telegram_check(None, None, lines.append)
    monkeypatch.setattr(requests, "get", lambda url, **k: R({"ok": True, "result": []}))
    lines.clear()
    assert not telegram_check(token, None, lines.append) and "press Start" in lines[0]
    assert not any(token in x for x in lines)


def test_telegram_commands_answer_only_the_owner_and_never_twice(monkeypatch, tmp_path):
    import requests

    from pumpfade.tgcmd import HELP, TelegramCommands, parse

    assert parse("/status@my_bot") == "status" and parse("상태") == "status" and parse("성과 보여줘") == "report"
    store = Store(tmp_path / "t.sqlite")
    updates = [
        {"update_id": 10, "message": {"chat": {"id": 4242}, "text": "/status"}},
        {"update_id": 11, "message": {"chat": {"id": 999}, "text": "/status"}},   # a stranger
        {"update_id": 12, "message": {"chat": {"id": 4242}, "text": "hello"}},
    ]
    seen_offsets, sent = [], []

    class R:
        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    def get(url, params=None, **k):
        seen_offsets.append(params.get("offset"))
        off = params.get("offset") or 0
        return R({"ok": True, "result": [u for u in updates if u["update_id"] >= off]})

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", lambda url, json=None, **k: sent.append(json) or R({"ok": True}))
    tc = TelegramCommands("123:" + "x" * 30, "4242", store, {"status": lambda: "equity $3,000", "report": lambda: "r"})
    assert tc.poll() == 2
    assert [m["text"] for m in sent] == ["equity $3,000", HELP] and all(m["chat_id"] == "4242" for m in sent)
    assert tc.poll() == 0 and seen_offsets[-1] == 13   # already answered: never repeated, even after a restart


def test_runner_status_text_lists_live_positions(env, tmp_path):
    from pumpfade.runner import Runner

    ex, store, eng, msgs = env
    eng.open_signals([cand()], DAY)
    ex.set_price(1.01)
    eng.tick()
    r = Runner(eng.settings, client=FakePublic(), store=store, notifier=type("N", (), {"send": lambda *a, **k: None})(),
               clock=lambda: ex.now, trader=ex)
    r.engine = eng
    text = r.status_text()
    assert "equity $" in text and "TGTUSDT (main, open)" in text and "closes in 48h" in text


def test_fills_and_misses_are_announced(env):
    ex, store, eng, msgs = env
    eng.open_signals([cand()], DAY)
    p = eng.book.all()[0]
    ex.partial_fill(p.entry_cid, 300)
    eng.tick()
    opened = [m for m in msgs if "SHORT opened" in m]
    assert len(opened) == 1 and "TGTUSDT" in opened[0] and "300 @ 1.005" in opened[0] and "time exit" in opened[0]
    ex.set_price(1.01)                 # the rest of the limit fills
    eng.tick()
    eng.tick()
    done = [m for m in msgs if "entry complete" in m]
    assert len(done) == 1 and "1194 @" in done[0]
    assert len([m for m in msgs if "SHORT" in m or "entry complete" in m]) == 2   # no repeats


def test_a_signal_that_never_fills_says_so(env):
    ex, store, eng, msgs = env
    eng.open_signals([cand()], DAY)
    real = ex.new_order
    ex.new_order = lambda **p: ({"status": "EXPIRED", "executedQty": "0"} if p.get("timeInForce") == "IOC"
                                else real(**p))  # the 10:00 IOC finds no bids within 0.3%
    ex.now = ENTRY + HOUR_MS + 30_000
    eng.tick()
    assert eng.book.all()[0].status == "failed" and any("no position" in m for m in msgs)


def test_heartbeat_pings_every_five_minutes(monkeypatch, tmp_path):
    import requests

    from pumpfade.runner import Runner

    pings = []
    monkeypatch.setattr(requests, "get", lambda url, **k: pings.append(url))
    s = Settings(db_path=tmp_path / "h.sqlite", watch_listings=False)
    s.heartbeat_url = "https://hc-ping.example/abc"
    now = [ENTRY + 2 * HOUR_MS]
    r = Runner(s, client=object(), store=Store(s.db_path), notifier=type("N", (), {"send": lambda *a, **k: None})(),
               clock=lambda: now[0])
    r.heartbeat(now[0])
    r.heartbeat(now[0] + 60_000)
    r.heartbeat(now[0] + 5 * 60_000)
    assert pings == ["https://hc-ping.example/abc"] * 2


def test_a_long_gap_in_the_loop_is_reported():
    from pumpfade.runner import keep_awake, pause_note

    assert pause_note(1_000_000.0, 1_000_000.0 + 60) is None
    msg = pause_note(1_763_400_000.0, 1_763_400_000.0 + 34 * 3600)
    assert "paused for 34.0h" in msg and "asleep" in msg
    assert keep_awake() is False          # not Windows here: nothing to do


def _fallback_at(env, px):
    ex, store, eng, msgs = env
    eng.open_signals([cand()], DAY)   # 09:00 open 1.0, limit 1.005
    ex.set_price(px)
    ex.now = ENTRY + HOUR_MS + 30_000
    eng.tick()
    return eng.book.all()[0]


def test_fallback_below_the_0900_open_aims_at_the_limits_take_profit(env):
    p = _fallback_at(env, 0.95)
    assert p.status == "open" and p.entry_avg == pytest.approx(0.95 * 0.999)
    assert p.tp_px == pytest.approx(0.8542, abs=1e-4)          # 1.005 × 0.85, not entry × 0.85 (≈ 0.8067)
    assert p.sl_px == pytest.approx(0.95 * 0.999 * 1.15, abs=1e-4)


def test_fallback_above_the_0900_open_takes_profit_15pct_below_its_entry(env):
    ex, store, eng, msgs = env
    p = _fallback_at(env, 1.003)       # above the open, under the 1.005 limit, so the limit never filled
    assert p.status == "open" and p.entry_avg == pytest.approx(1.003 * 0.999)
    assert p.tp_px == pytest.approx(1.003 * 0.999 * 0.85, abs=1e-4)


def test_no_fallback_entry_when_the_price_is_already_at_the_0900_take_profit(env):
    ex, store, eng, msgs = env
    p = _fallback_at(env, 0.85)
    assert p.status == "failed" and ex.pos == 0 and "already at the 09:00 take-profit" in p.note
    assert any("no position" in m for m in msgs)


def test_weaker_long_short_ratio_takes_half_size(env):
    ex, store, eng, msgs = env
    weak = Candidate("WEAKUSDT", DAY, 0.25, 150, 0.05, 0.0, ratio=1.8, funding=0.0, funding_interval_h=4, passed=True)
    edge = Candidate("EDGEUSDT", DAY, 0.25, 150, 0.05, 0.0, ratio=2.0, funding=0.0, funding_interval_h=4, passed=True)
    eng.open_signals([cand(), weak, edge], DAY)
    by = {p.symbol: p for p in eng.book.all()}
    assert by[SYM].margin == pytest.approx(1200)                      # ratio 2.5: full 12%
    assert by["WEAKUSDT"].margin == pytest.approx(600)                # ratio 1.8: 6%
    assert by["EDGEUSDT"].margin == pytest.approx(600)                # exactly 2.0 is not above the line


def test_position_size_is_capped_for_large_accounts(env):
    ex, store, eng, msgs = env
    ex.cash = 200_000                  # 12% would be $24,000
    eng._account_cache = None
    eng.open_signals([cand()], DAY)
    assert eng.book.all()[0].margin == pytest.approx(10_000)


def test_a_failed_time_exit_keeps_the_stop_in_place(env):
    ex, store, eng, msgs = env
    eng.open_signals([cand()], DAY)
    ex.set_price(1.01)
    eng.tick()
    p = eng.book.all()[0]
    real = ex.new_order

    def down(**kw):
        if kw.get("type") == "MARKET":
            raise RuntimeError("network down")
        return real(**kw)

    ex.new_order = down
    ex.now = p.deadline_ms + MIN
    eng.tick()                          # the market buy fails
    assert eng.book.all()[0].status == "closing" and ex.pos != 0
    assert open_algos(ex) == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]   # still protected
    ex.new_order = real
    ex.now += 3 * MIN
    eng.tick()                          # retried after two minutes
    settle(eng, ex)
    p = eng.book.all()[0]
    assert ex.pos == 0 and p.status == "closed" and p.exit_reason == "time" and open_algos(ex) == []


class LongPublic:
    """Hourly open 1.0 at the entry, then a 1m path given per test."""
    def __init__(self, path):
        self.path = path   # list of (minute offset, high, low, close)

    def klines(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
        if interval == "1h":
            return [[ENTRY, "1.0", "1.0", "1.0", "1.0", "0", ENTRY + HOUR_MS - 1, "0"]]
        out = []
        for k, h, l, c in self.path:
            t = ENTRY + k * MIN
            if t >= start_ms and (end_ms is None or t <= end_ms):
                out.append([t, str(c), str(h), str(l), str(c), "0", t + MIN - 1, "0"])
        return out[:limit]


def _paper(tmp_path, path, now):
    from pumpfade.paperlong import PaperLongs
    clock = [now]
    msgs = []
    pl = PaperLongs(LongPublic(path), Store(tmp_path / "pl.sqlite"), notify=lambda m, **k: msgs.append(m),
                    clock=lambda: clock[0])
    return pl, msgs, clock


def test_paper_long_only_on_red_days_and_takes_profit(tmp_path):
    pl, msgs, clock = _paper(tmp_path, [(0, 1.01, 0.99, 1.0), (30, 1.09, 1.0, 1.08), (60, 1.1, 1.05, 1.06)],
                             ENTRY + 2 * HOUR_MS)
    assert pl.open([cand()], DAY, majors=-0.01) == []          # not a red day
    notes = pl.open([cand()], DAY, majors=-0.05)
    assert len(notes) == 1 and "paper long" in notes[0]
    pl.tick()
    r = pl.db.execute("SELECT status, why, ret FROM paper_longs").fetchone()
    assert r[0] == "closed" and r[1] == "take-profit" and r[2] == pytest.approx(0.08 - 0.0005 - 0.0002 * 1.08, abs=1e-4)
    assert any("✅" in m for m in msgs) and "1 closed" in pl.summary()


def test_paper_long_stop_wins_a_tie_and_time_exit(tmp_path):
    pl, msgs, clock = _paper(tmp_path, [(10, 1.2, 0.6, 0.7)], ENTRY + HOUR_MS)
    pl.open([cand()], DAY, majors=-0.04)
    pl.tick()
    assert pl.db.execute("SELECT why FROM paper_longs").fetchone()[0] == "stop"
    (tmp_path / "b").mkdir()
    pl2, msgs2, clock2 = _paper(tmp_path / "b", [(k, 1.02, 0.98, 1.01) for k in range(0, 72 * 60, 60)],
                                ENTRY + 73 * HOUR_MS)
    pl2.open([cand()], DAY, majors=-0.04)
    pl2.tick()
    r = pl2.db.execute("SELECT status, why FROM paper_longs").fetchone()
    assert tuple(r) == ("closed", "3 days")


class ScanPublic:
    """Daily bars for a /scan preview: PUMPUSDT +25%, NEARUSDT +15%, the majors flat, 120 filler coins."""

    def __init__(self, now):
        self.now = now
        self.day = now // DAY_MS
        self.coins = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "PUMPUSDT", "NEARUSDT"] + [f"F{i}USDT" for i in range(120)]

    def exchange_info(self):
        return [{"symbol": s, "contractType": "PERPETUAL", "status": "TRADING", "underlyingType": "COIN"}
                for s in self.coins]

    def klines(self, symbol, interval, start_ms=None, end_ms=None, limit=500):
        vol = 1.0 if symbol in ("PUMPUSDT", "NEARUSDT") else 1e9
        last = {"PUMPUSDT": 1.25, "NEARUSDT": 1.15}.get(symbol, 1.0)
        rows = []
        for d in range(self.day - 32, self.day + 1):
            c = last if d == self.day else 1.0
            rows.append([d * DAY_MS, "1.0", str(c), "1.0", str(c), "0", d * DAY_MS + DAY_MS - 1, str(vol)])
        return rows

    def funding_intervals(self):
        return {}

    def long_short_ratio(self, symbol, end_ms):
        return 2.4, self.now - 60_000

    def funding_history(self, symbol, end_ms=None, limit=5, start_ms=None):
        return [{"fundingTime": self.now - HOUR_MS, "fundingRate": "0.0001"}]


def test_scan_command_previews_the_next_0900_pick(env):
    from pumpfade.runner import Runner

    ex, store, eng, msgs = env
    now = (DAY + 1) * DAY_MS + 22 * HOUR_MS          # 07:00 KST
    pub = ScanPublic(now)
    r = Runner(eng.settings, client=pub, store=store, notifier=type("N", (), {"send": lambda *a, **k: None})(),
               clock=lambda: now, trader=ex)
    text = r.scan_text()
    assert "✅ PUMPUSDT +25.0%" in text and "롱숏 2.40 → 12%" in text
    assert "근접: NEARUSDT +15.0%" in text and "메이저 평균 +0.00%" in text
    r.clock = lambda: (DAY + 2) * DAY_MS - 5 * 60_000  # 08:55 KST: busy with the 09:00 entry
    assert "09:10" in r.scan_text()
    from pumpfade.tgcmd import parse
    assert parse("스캔") == parse("/scan") == "scan"

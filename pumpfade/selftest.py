"""Exercise the live order path against Binance (testnet by default) before trusting it with money.

Without --fill nothing can execute: a far-away limit order and a far-away stop are placed and cancelled.
With --fill (default on testnet) a minimum-size short is opened with an IOC sell, protected with the same
closePosition STOP_MARKET / TAKE_PROFIT_MARKET algo orders the bot uses, then closed at market and cleaned up.
"""

from __future__ import annotations

import time

from .trader import BinanceTrader


def diagnose(trader, equity: float | None, out=print) -> bool:
    """Key permissions and where the money is, before any order is sent. False when trading cannot work."""
    good = True
    if getattr(trader, "spot_http", None) is not None:
        try:
            perms = trader.api_restrictions()
        except Exception as exc:
            perms = None
            out(f"  SKIP key permissions could not be read: {exc}")
        if perms is not None:
            on = {k: bool(perms.get(k)) for k in ("enableReading", "enableFutures", "enableWithdrawals", "ipRestrict",
                                                  "permitsUniversalTransfer", "enableInternalTransfer")}
            if on["enableFutures"]:
                out("  OK   key permission: Enable Futures is on")
            else:
                good = False
                out("  FAIL key permission: Enable Futures is OFF -> Binance API Management, Edit restrictions, "
                    "tick Enable Futures, Save")
            if on["enableWithdrawals"]:
                good = False
                out("  FAIL key permission: withdrawals are ON -> turn Enable Withdrawals off")
            if on["permitsUniversalTransfer"] or on["enableInternalTransfer"]:
                out("  WARN key permission: transfers are allowed; the bot does not need them, turn them off")
            out("  OK   key is IP-restricted" if on["ipRestrict"] else
                "  WARN key is not IP-restricted; restrict it to this computer's public IP")
        try:
            out(f"  INFO spot wallet of this key's account: {trader.spot_balance('USDT'):,.2f} USDT")
        except Exception:
            pass
    if equity is not None and equity <= 0:
        good = False
        out("  FAIL the futures wallet of this key's account is empty -> move USDT into the USDⓈ-M Futures wallet "
            "of the account the key belongs to (a key made on the master account cannot see the sub-account)")
    return good


def run(trader: BinanceTrader, symbol: str, fill: bool, out=print) -> bool:
    ok = True

    def step(name, fn):
        nonlocal ok
        try:
            res = fn()
            out(f"  OK   {name}" + (f": {res}" if res not in (None, "") else ""))
            return res
        except Exception as exc:
            ok = False
            out(f"  FAIL {name}: {exc}")
            return None

    out(f"selftest on {trader.base_url} with {symbol}")
    step("server time", lambda: trader.sync_time() or f"clock offset {trader.offset_ms} ms")
    dual = step("position mode", lambda: "hedge mode (switch to one-way!)" if trader.dual_side_position() else "one-way")
    if dual and "hedge" in dual:
        ok = False
    equity = None

    def read_equity():
        nonlocal equity
        equity = float(trader.account()["totalMarginBalance"])
        return f"equity {equity:,.2f} USDT"

    step("futures wallet", read_equity)
    if not diagnose(trader, equity, out):
        out("SOMETHING FAILED: fix the items above, then run the selftest again (no orders were sent)")
        return False
    f = step("symbol filters", lambda: trader.filters(symbol))
    if f is None:
        return False
    step("isolated margin, 1x", lambda: trader.prepare_symbol(symbol, 1))
    bid, ask = trader.book_top(symbol)
    qty = f.qty(max(float(f.min_notional) * 1.2 / bid, float(f.min_qty)))
    if f.qty(float(qty)) < f.min_qty:
        qty = f.min_qty
    tag = int(time.time()) % 100000
    far = f.price(ask * 1.03, up=True)
    step(f"place far limit sell {qty} @ {far}", lambda: trader.new_order(
        symbol=symbol, side="SELL", type="LIMIT", timeInForce="GTC", quantity=qty, price=far,
        newClientOrderId=f"pf-selftest-L{tag}")["status"])
    step("query it by client id", lambda: (trader.query_order(symbol, f"pf-selftest-L{tag}") or {}).get("status", "not found"))
    step("cancel it", lambda: (trader.cancel_order(symbol, f"pf-selftest-L{tag}") or {}).get("status"))
    stop_far = f.price(ask * 1.10, up=True)
    step(f"place algo STOP_MARKET (reduce-only) @ {stop_far}", lambda: trader.new_algo(
        symbol=symbol, side="BUY", type="STOP_MARKET", triggerPrice=stop_far, quantity=qty, reduceOnly="true",
        workingType="CONTRACT_PRICE", clientAlgoId=f"pf-selftest-A{tag}").get("algoStatus"))
    step("query it", lambda: (trader.query_algo(f"pf-selftest-A{tag}") or {}).get("algoStatus"))
    step("cancel it", lambda: trader.cancel_algo(f"pf-selftest-A{tag}"))
    if fill:
        price = f.price(bid * 0.997, up=True)
        res = step(f"open a minimum short: IOC sell {qty} @ {price}", lambda: trader.new_order(
            symbol=symbol, side="SELL", type="LIMIT", timeInForce="IOC", quantity=qty, price=price,
            newClientOrderId=f"pf-selftest-O{tag}"))
        filled = float(res["executedQty"]) if res else 0.0
        if filled <= 0:
            out("  FAIL nothing filled; skipping the protection and close checks")
            return False
        entry = float(res["avgPrice"])
        step("protect: STOP_MARKET closePosition +15%", lambda: trader.new_algo(
            symbol=symbol, side="BUY", type="STOP_MARKET", triggerPrice=f.price(entry * 1.15, up=True),
            closePosition="true", workingType="CONTRACT_PRICE", clientAlgoId=f"pf-selftest-S{tag}").get("algoStatus"))
        step("protect: TAKE_PROFIT_MARKET closePosition −15%", lambda: trader.new_algo(
            symbol=symbol, side="BUY", type="TAKE_PROFIT_MARKET", triggerPrice=f.price(entry * 0.85, up=False),
            closePosition="true", workingType="CONTRACT_PRICE", clientAlgoId=f"pf-selftest-T{tag}").get("algoStatus"))
        step("position shows up", lambda: trader.positions().get(symbol))
        step("cancel protection", lambda: (trader.cancel_algo(f"pf-selftest-S{tag}"), trader.cancel_algo(f"pf-selftest-T{tag}")) and "")
        step("close at market (reduce-only)", lambda: trader.new_order(
            symbol=symbol, side="BUY", type="MARKET", quantity=f.qty(filled), reduceOnly="true",
            newClientOrderId=f"pf-selftest-C{tag}")["status"])
        time.sleep(1)
        left = trader.positions().get(symbol, 0.0)
        out(f"  {'OK  ' if not left else 'FAIL'} position after close: {left}")
        ok = ok and not left
        step("fills are readable", lambda: f"{len(trader.user_trades(symbol, int(time.time() * 1000) - 600_000))} fills")
    out("ALL OK" if ok else "SOMETHING FAILED: do not run live until it passes")
    return ok

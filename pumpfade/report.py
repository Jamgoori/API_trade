"""Paper results per book, next to what the backtest expects (research/STRATEGY.md)."""

from __future__ import annotations

from collections import defaultdict

from .store import Store

# Backtest 2024-01..2026-10, rule as written (research/STRATEGY.md, README 20 and 22)
EXPECTED = {"per_signal": 0.021, "win": 0.63, "tp": 0.37, "stop": 0.31, "time": 0.32, "limit_fill": 0.83}


def book_stats(store: Store, book: str, start_equity: float) -> dict:
    legs = store.legs("book=?", (book,))
    closed = [lg for lg in legs if lg.status == "closed"]
    signals = defaultdict(list)
    for lg in legs:
        if lg.status != "cancelled":
            signals[(lg.symbol, lg.signal_day)].append(lg)
    done = {k: v for k, v in signals.items() if all(lg.status == "closed" for lg in v)}
    rets = [sum(lg.pnl for lg in v) / v[0].margin for v in done.values()]
    reasons = defaultdict(int)
    for lg in closed:
        reasons[lg.exit_reason] += 1
    market_slips = [lg.entry_slip for lg in legs if lg.kind in ("market", "fallback") and lg.entry_slip is not None]
    stop_slips = [lg.exit_slip for lg in closed if lg.exit_reason == "stop" and lg.exit_slip is not None]
    limit_legs = [lg for lg in legs if lg.part == "B" and lg.status != "pending"]
    filled = [lg for lg in limit_legs if lg.kind == "limit"]
    eq = store.book_equity(book, start_equity)
    curve = [start_equity] + [e for _, e in store.equity_log(book)]
    peak, maxdd = curve[0], 0.0
    for e in curve:
        peak = max(peak, e)
        maxdd = min(maxdd, e / peak - 1)
    n = len(closed) or 1
    return {
        "book": book, "equity": eq, "return": eq / start_equity - 1, "maxdd": maxdd,
        "signals": len(signals), "closed_signals": len(done),
        "per_signal": sum(rets) / len(rets) if rets else None,
        "win": sum(r > 0 for r in rets) / len(rets) if rets else None,
        "tp": reasons["take-profit"] / n if closed else None, "stop": reasons["stop"] / n if closed else None,
        "time": reasons["time"] / n if closed else None,
        "market_slip": sum(market_slips) / len(market_slips) if market_slips else None,
        "stop_slip": sum(stop_slips) / len(stop_slips) if stop_slips else None,
        "limit_fill": len(filled) / len(limit_legs) if limit_legs else None,
        "funding": sum(lg.funding for lg in legs), "fees": sum(lg.fees for lg in legs),
        "open": sum(lg.status in ("open", "pending") for lg in legs),
    }


def _p(x, d=1):
    return "—" if x is None else f"{x * 100:+.{d}f}%"


def _r(x):
    return "—" if x is None else f"{x * 100:.0f}%"


def render(store: Store, books: list[str], start_equity: float) -> str:
    lines = ["book | equity | return | max DD | signals (closed) | per signal | win | TP/stop/time | "
             "market slip | stop slip | limit fill | funding | open"]
    for b in books:
        s = book_stats(store, b, start_equity)
        lines.append(
            f"{b} | ${s['equity']:,.0f} | {_p(s['return'])} | {_p(s['maxdd'])} | {s['signals']} ({s['closed_signals']}) | "
            f"{_p(s['per_signal'], 2)} | {_r(s['win'])} | {_r(s['tp'])}/{_r(s['stop'])}/{_r(s['time'])} | "
            f"{_p(s['market_slip'], 2)} | {_p(s['stop_slip'], 2)} | {_r(s['limit_fill'])} | ${s['funding']:+,.2f} | {s['open']}")
    e = EXPECTED
    lines.append(f"backtest | | | | | {_p(e['per_signal'], 2)} | {_r(e['win'])} | {_r(e['tp'])}/{_r(e['stop'])}/{_r(e['time'])} "
                 f"| ≤ +0.30% assumed | | {_r(e['limit_fill'])} | | ")
    return "\n".join(lines)

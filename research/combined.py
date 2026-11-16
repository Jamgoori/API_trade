"""Three short sleeves on one account: the main rule, listing pumps and OI events.

- main:    research/STRATEGY.md (daily +20% close, filters, +0.5% limit then 01:00 UTC market, TP −15% / stop +15% / 48h,
           drop a coin after a stop)
- listing: an existing Binance perp gets a new Upbit / Bithumb KRW market or Binance spot pair; short the perp one
           hour after the listing opens, TP −15% / stop +15% / 48h
- oi:      4h +20% pump with 5x volume, open interest +50% in 4h and long/short ratio > 1.5; short at the next hour,
           TP −15% / stop +30% / 7 days

Each sleeve's trades are simulated on their own, then one portfolio takes them in time order: one position per coin,
margin = share of equity, at most 90% of equity committed. Equity is marked to market daily.
"""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from funding_filter import funding_at_entry, skip_rule
from limit_entry import variant
from short_study import H_DAY, load, pct, volume_ranks
from stop_and_hold import load_signals
from wick_filter import day_shape

HERE = Path(__file__).resolve().parent
PERIODS = {"2024-01..2025-06": ("2024-01-01", "2025-06-30"), "2025-07..2026-10": ("2025-07-01", "2026-10-31"),
           "2024-01..2026-10": ("2024-01-01", "2026-10-31")}


def sleeve_trades(name, sig, syms, tp, stop, hold, ban):
    """Trades one sleeve would take on its own: (sleeve, sym, entry_h, exit_h, ret, daily mark-to-market)."""
    sig = sig.sort_values("entry_h").reset_index(drop=True)
    P = g.Paths(syms, sig, g.MAX_H if hold <= 30 else hold * H_DAY)
    ret, j, why, mtm = P.outcome(tp, stop, hold)
    out, state = [], {}
    for r in range(len(sig)):
        if np.isnan(ret[r]):
            continue
        sym = sig.sym.iat[r]
        free, banned = state.get(sym, (-1, False))
        if banned or P.entry_h[r] <= free:
            continue
        state[sym] = (P.entry_h[r] + j[r], ban and why[r] >= 2)
        out.append((name, sym, int(P.entry_h[r]), int(P.entry_h[r] + j[r]), float(ret[r]), mtm[r].copy()))
    return out


def portfolio(trades, day_a, day_b, frac, sleeves):
    trades = sorted([t for t in trades if t[0] in sleeves and day_a * H_DAY <= t[2] < (day_b + 1) * H_DAY],
                    key=lambda t: t[2])
    by_day = {}
    for t in trades:
        by_day.setdefault(t[2] // H_DAY, []).append(t)
    cash, open_, curve, taken = 1.0, [], [], {s: 0 for s in sleeves}
    slots = int(0.9 / frac + 1e-9)
    for day in range(day_a, day_b + 1):
        keep = []
        for p in open_:
            if p["exit_day"] < day:
                cash += p["m"] * (1 + p["ret"])
            else:
                keep.append(p)
        open_ = keep

        def val(p, d):
            if p["exit_day"] <= d:
                return p["ret"]
            k = d - p["entry_day"]
            return p["mtm"][k] if 0 <= k < len(p["mtm"]) and not np.isnan(p["mtm"][k]) else 0.0

        equity = cash + sum(p["m"] * (1 + val(p, day - 1)) for p in open_)
        for t in by_day.get(day, []):
            if len(open_) >= slots or any(p["sym"] == t[1] for p in open_):
                continue
            m = equity * frac
            cash -= m
            open_.append({"m": m, "sym": t[1], "ret": t[4], "mtm": t[5], "entry_day": t[2] // H_DAY,
                          "exit_day": t[3] // H_DAY})
            taken[t[0]] += 1
        curve.append(cash + sum(p["m"] * (1 + val(p, day)) for p in open_))
    eq = np.array(curve)
    return eq[-1] * 10_000, float((eq / np.maximum.accumulate(eq) - 1).min()), taken


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    da = int(pd.Timestamp("2024-01-01").timestamp() // 86400)
    out = ["## Three sleeves on one account ($10k, marked to market daily)\n"]
    for slip, stop_slip in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP, g.STOP_SLIP = slip, stop_slip
        # main
        sig = load_signals(syms, ranks, da, last)
        sig["retrace"] = [day_shape(syms[r.sym], r.day)[1] for r in sig.itertuples()]
        sig["skipf"] = [skip_rule(*funding_at_entry(syms[r.sym], (r.day + 1) * H_DAY)) for r in sig.itertuples()]
        sig = sig[(sig.ls > 1.5) & (sig.majors <= 0.07) & (sig.majors >= -0.03) & (sig.retrace <= 0.30) & ~sig.skipf]
        main_sig, _ = variant(syms, sig.reset_index(drop=True), 0.005, 1, True, slip)
        trades = sleeve_trades("main", main_sig, syms, 0.15, 0.15, 2, True)
        # listing
        ls = pd.read_csv(HERE / "results" / "listings.csv", parse_dates=["listed"])
        hours = (ls.listed - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(hours=1)
        lst = pd.DataFrame({"sym": ls.perp, "entry_h": hours.astype(int) + 1})
        lst = lst[[s in syms and syms[s].t0 <= h < syms[s].t0 + syms[s].n for s, h in zip(lst.sym, lst.entry_h)]]
        lst["day"] = lst.entry_h // H_DAY - 1
        trades += sleeve_trades("listing", lst, syms, 0.15, 0.15, 2, False)
        # oi
        ev = pd.read_csv(HERE / "results" / "events_oi.csv").dropna(subset=["oi_4h", "ls_accounts"])
        ev = ev[(ev.oi_4h > 0.5) & (ev.ls_accounts > 1.5)]
        oi = pd.DataFrame({"sym": ev.sym, "entry_h": ev.abs_h + 1})
        oi["day"] = oi.entry_h // H_DAY - 1
        trades += sleeve_trades("oi", oi, syms, 0.15, 0.30, 7, False)

        out += [f"### Slippage {slip:.1%} (stop {stop_slip:.1%})\n",
                "| sleeves | margin / trade | " + " | ".join(PERIODS) + " | trades taken (whole period) |",
                "|---|---|" + "---|" * len(PERIODS) + "---|"]
        for sleeves in (("main",), ("listing",), ("oi",), ("main", "listing"), ("main", "listing", "oi")):
            for frac in (0.10, 0.15):
                cells, taken = [], None
                for pname, (a, b) in PERIODS.items():
                    pa = int(pd.Timestamp(a).timestamp() // 86400)
                    pb = min(int(pd.Timestamp(b).timestamp() // 86400), last)
                    fin, dd, tk = portfolio(trades, pa, pb, frac, sleeves)
                    cells.append(f"${fin:,.0f} ({pct(dd, 0)})")
                    taken = tk
                out.append(f"| {' + '.join(sleeves)} | {frac:.0%} | " + " | ".join(cells) + " | "
                           + ", ".join(f"{k} {v}" for k, v in taken.items()) + " |")
                print(out[-1], flush=True)
        out.append("")
    g.SLIP, g.STOP_SLIP = 0.001, 0.005
    (HERE / "results" / "combined.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

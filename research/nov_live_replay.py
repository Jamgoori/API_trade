"""Replay of the live period with today's rules: $10k on the first live day (entry 2026-11-17 09:00 KST), main rule
(L/S 1.5-3.0, 6%/12%, +0.5% limit then 10:00 IOC, fallback take-profit) and listing shorts, max $10k per trade,
90% margin cap, no second short on a coin within 72h. Trades still open at the end of the data are marked there."""

import sys

import numpy as np
import pandas as pd

import tp_grid as g
from funding_filter import funding_at_entry, skip_rule
from limit_entry import variant
from majors_down import tp_rows
from nov_extend import extended
from short_study import H_DAY, pct, volume_ranks
from stop_and_hold import load_signals
from wick_filter import day_shape

LAST = sys.argv[1] if len(sys.argv) > 1 else "2026-11-24"
FIRST_SIGNAL = "2026-11-16"            # entry 2026-11-17 00:00 UTC (09:00 KST)
LISTINGS = [("ONUSDT", "2026-11-24 05:00", "upbit"), ("ONUSDT", "2026-11-24 06:00", "bithumb"),
            ("SQDUSDT", "2026-11-24 08:00", "bithumb")]   # first trading hour, UTC (KNTQ has no perp)


def run(slip):
    g.SLIP, g.STOP_SLIP = slip, 0.01
    syms = extended(LAST)
    d0, ranks, _ = volume_ranks(syms)
    end_h = max(s.t0 + s.n for s in syms.values())
    da = int(pd.Timestamp(FIRST_SIGNAL).timestamp() // 86400)
    db = end_h // H_DAY - 2
    sig = load_signals(syms, ranks, da, db)
    sig["retrace"] = [day_shape(syms[r.sym], r.day)[1] for r in sig.itertuples()]
    sig["skipf"] = [skip_rule(*funding_at_entry(syms[r.sym], (r.day + 1) * H_DAY)) for r in sig.itertuples()]
    allsig = sig.copy()
    sig = sig[(sig.ls > 1.5) & (sig.ls <= 3.0) & (sig.majors <= 0.07) & (sig.majors >= -0.03) & (sig.retrace <= 0.30)
              & ~sig.skipf].reset_index(drop=True)
    m, _ = variant(syms, sig, 0.005, 1, True, slip)
    tp = tp_rows(m, syms, slip)
    m["tp"] = tp
    m = m[~np.isnan(m.tp)].reset_index(drop=True)
    m["sleeve"] = "main"
    m["frac"] = np.where(m.ls > 2.0, 0.12, 0.06)
    lst = pd.DataFrame([{"sym": s, "entry_h": int(pd.Timestamp(t, tz="UTC").timestamp() // 3600) + 1, "venue": v}
                        for s, t, v in LISTINGS])
    lst["day"] = lst.entry_h // H_DAY - 1
    lst["tp"], lst["sleeve"], lst["frac"] = 0.15, "listing", 0.12
    rows = pd.concat([m, lst], ignore_index=True)
    rows = rows[rows.entry_h < end_h].reset_index(drop=True)
    P = g.Paths(syms, rows, g.MAX_H)
    ret, j, why = np.full(len(rows), np.nan), np.zeros(len(rows), int), np.zeros(len(rows), int)
    for t in sorted(set(rows.tp)):
        r, jj, w, _ = P.outcome(float(t), 0.15, 2, truncate=True)
        sel = (rows.tp == t).to_numpy()
        ret[sel], j[sel], why[sel] = r[sel], jj[sel], w[sel]
    rows["ret"], rows["exit_h"], rows["why"] = ret, rows.entry_h + j, why
    rows["open_at_end"] = rows.exit_h >= end_h - 1
    rows = rows.sort_values("entry_h").reset_index(drop=True)

    cash, open_, last_short, log = 10_000.0, [], {}, []
    for r in rows.itertuples():
        for p in [p for p in open_ if p["exit_h"] <= r.entry_h]:
            cash += p["m"] * p["ret"]
            open_.remove(p)
        eq = cash
        if any(p["sym"] == r.sym for p in open_):
            log.append((r, "skip: holding")); continue
        if r.sym in last_short and r.entry_h - last_short[r.sym] < 72:
            log.append((r, "skip: shorted in 72h")); continue
        mgn = min(eq * r.frac, 10_000)
        if sum(p["m"] for p in open_) + mgn > 0.9 * eq:
            log.append((r, "skip: margin")); continue
        if np.isnan(r.ret):
            log.append((r, "no data")); continue
        open_.append({"sym": r.sym, "m": mgn, "ret": r.ret, "exit_h": r.exit_h})
        last_short[r.sym] = r.entry_h
        log.append((r, f"${mgn:,.0f}"))
    final = cash + sum(p["m"] * p["ret"] for p in open_)
    return rows, log, final, allsig, end_h


def main():
    names = {0: "48h", 1: "TP", 2: "stop", 3: "liq"}
    for slip in (0.001, 0.003):
        rows, log, final, allsig, end_h = run(slip)
        print(f"\n## slippage {slip:.1%}: $10,000 -> ${final:,.0f} ({final / 1e4 - 1:+.1%}), data to "
              f"{pd.Timestamp(end_h * 3600, unit='s', tz='Asia/Seoul'):%m-%d %H:%M} KST")
        for r, note in log:
            kst = pd.Timestamp(r.entry_h * 3600, unit="s", tz="Asia/Seoul")
            res = "" if np.isnan(r.ret) else f"{pct(r.ret)} {'open (marked)' if r.open_at_end else names[r.why]}"
            ls = f" L/S {r.ls:.2f}" if r.sleeve == "main" else f" ({r.venue})"
            print(f"{kst:%m-%d %H:%M} {r.sym:<14} {r.sleeve:<8}{ls} {res:<22} {note}")
    print("\nall +20% signals in the window (before filters):")
    print(allsig[["sym", "day", "ls", "majors", "retrace", "rank"]].assign(
        day=lambda d: pd.to_datetime((d.day + 1) * 86400, unit="s").dt.strftime("%m-%d")).to_string(index=False))


if __name__ == "__main__":
    main()

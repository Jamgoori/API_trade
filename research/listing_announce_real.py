"""Upbit / Bithumb KRW listings: short after the announcement vs after trading starts, with real announcement times
(results/krw_announcements.csv, from krw_announcements.py). Exits as now: TP −15% / stop +15% / 48h,
slippage 0.3% (stop 1%). Only listings announced at least an hour before trading opens.
"""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from listing_timing import outcome
from short_study import load, pct

HERE = Path(__file__).resolve().parent
SPLIT = pd.Timestamp("2025-07-01").timestamp() // 3600


def main() -> None:
    syms = load()
    g.SLIP, g.STOP_SLIP = 0.003, 0.01
    a = pd.read_csv(HERE / "results" / "krw_announcements.csv")
    a = a[a.announced.notna() & a.perp.isin(syms.keys())].copy()
    for c in ("listed", "announced"):
        a[c] = pd.to_datetime(a[c], utc=True)
    epoch = pd.Timestamp(0, tz="UTC")
    a["h0"] = ((a.listed - epoch) // pd.Timedelta(hours=1)).astype(int)
    a["ha"] = ((a.announced - epoch) // pd.Timedelta(hours=1)).astype(int)
    # an article more than 12h before trading is a delayed or misdated listing (one case)
    a = a[(a.ha + 1 <= a.h0) & (a.listed - a.announced <= pd.Timedelta(hours=12))].reset_index(drop=True)
    lag = (a.listed - a.announced) / pd.Timedelta(hours=1)
    out = ["## KRW listings with real announcement times: short after the announcement vs after trading starts\n",
           f"{len(a)} listings announced 1h+ before trading (hours from announcement to trading: median {lag.median():.1f})\n"]
    for venue in ("upbit", "bithumb", None):
        v = a if venue is None else a[a.venue == venue]
        out += [f"### {venue or 'both'} ({len(v)})\n", "| entry | trades | per trade | win | early / late | worst |",
                "|---|---|---|---|---|---|"]
        for label, col, k in (("announcement, next hour open (0-60 min after)", "ha", 1),
                              ("announcement +2h", "ha", 2), ("announcement +3h", "ha", 3),
                              ("trading opens", "h0", 0), ("trading opens +1h (now)", "h0", 1)):
            d, ret = outcome(syms, pd.DataFrame({"sym": v.perp, "entry_h": v[col] + k}))
            ok = ~np.isnan(ret)
            early = (d.entry_h < SPLIT).to_numpy()
            out.append(f"| {label} | {ok.sum()} | {pct(ret[ok].mean())} | {np.mean(ret[ok] > 0):.0%} | "
                       f"{pct(ret[ok & early].mean())} / {pct(ret[ok & ~early].mean())} | {pct(ret[ok].min(), 0)} |")
        out.append("")
    # price path: announcement hour open -> next hour open -> trading opens +1h
    jump, after = [], []
    for r in a.itertuples():
        s = syms[r.perp]
        i, j = r.ha - s.t0, r.h0 + 1 - s.t0
        if 0 <= i and j < s.n:
            jump.append(s.open[i + 1] / s.open[i] - 1)
            after.append(s.open[j] / s.open[i + 1] - 1)
    jump, after = np.array(jump), np.array(after)
    out += [f"Announcement hour move (its open to the next hour's open): median {pct(np.median(jump))}; "
            f"then to trading opens +1h: median {pct(np.median(after))}, higher in {np.mean(after > 0):.0%}, "
            f"+15% or more higher in {np.mean(after >= 0.15):.0%}\n"]
    print("\n".join(out), flush=True)
    (HERE / "results" / "listing_announce_real.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

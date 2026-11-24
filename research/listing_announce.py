"""Upbit / Bithumb KRW listings: short right after the announcement pump instead of after trading starts?

There is no history of KRW announcement times, but the perp usually jumps in the hour the notice comes out. The
announcement hour is taken as the first hour in the 6 hours before trading opens whose candle closes 8% (or 15%)
above its open; a bot that read the notice would short at the next hour's open (announcement +1h) or one hour later.
Compared on the same listings with the entry used now (first trading hour +1h). Exits as now: TP −15% / stop +15% /
48h, slippage 0.3% (stop 1%).
"""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from listing_timing import outcome
from short_study import load, pct

HERE = Path(__file__).resolve().parent
SPLIT = pd.Timestamp("2025-07-01").timestamp() // 3600


def spike_hour(s, h0, th):
    for h in range(h0 - 6, h0):
        i = h - s.t0
        if 0 <= i < s.n and s.close[i] / s.open[i] - 1 >= th:
            return h
    return None


def main() -> None:
    syms = load()
    g.SLIP, g.STOP_SLIP = 0.003, 0.01
    ev = pd.read_csv(HERE / "results" / "listings.csv", parse_dates=["listed"])
    ev["listed"] = pd.to_datetime(ev.listed, utc=True)
    ev["h"] = ((ev.listed - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(hours=1)).astype(int)
    ev = ev[ev.venue.isin(["upbit", "bithumb"]) & ev.perp.isin(syms.keys())].reset_index(drop=True)
    out = ["## Upbit / Bithumb listings: short after the announcement pump vs after trading starts\n"]
    for th in (0.08, 0.15):
        ev["sh"] = [spike_hour(syms[r.perp], r.h, th) for r in ev.itertuples()]
        hit = ev[ev.sh.notna()].copy()
        hit["sh"] = hit.sh.astype(int)
        lead = hit.h - hit.sh
        out += [f"### Announcement candle = first hourly candle +{th:.0%} or more in the 6h before trading: "
                f"{len(hit)} of {len(ev)} listings (hours before trading: median {lead.median():.0f})\n",
                "| entry (same listings) | trades | per trade | win | early / late | worst |", "|---|---|---|---|---|---|"]
        for label, col, k in (("announcement +1h", "sh", 1), ("announcement +2h", "sh", 2),
                              ("trading opens", "h", 0), ("trading opens +1h (now)", "h", 1)):
            d, ret = outcome(syms, pd.DataFrame({"sym": hit.perp, "entry_h": hit[col] + k}))
            ok = ~np.isnan(ret)
            early = (d.entry_h < SPLIT).to_numpy()
            out.append(f"| {label} | {ok.sum()} | {pct(ret[ok].mean())} | {np.mean(ret[ok] > 0):.0%} | "
                       f"{pct(ret[ok & early].mean())} / {pct(ret[ok & ~early].mean())} | {pct(ret[ok].min(), 0)} |")
        # how far the price kept running between the announcement +1h entry and the trading-start entry
        run = [syms[r.perp].open[r.h + 1 - syms[r.perp].t0] / syms[r.perp].open[r.sh + 1 - syms[r.perp].t0] - 1
               for r in hit.itertuples() if r.h + 1 - syms[r.perp].t0 < syms[r.perp].n]
        out += [f"\nPrice change from announcement +1h to trading opens +1h: median {pct(np.median(run))}, "
                f"higher in {np.mean(np.array(run) > 0):.0%} of listings\n"]
        print("\n".join(out[-9:]), flush=True)
    (HERE / "results" / "listing_announce.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

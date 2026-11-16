"""Listing pumps of coins that already traded as Binance perps: what the perp does around a new listing,
and whether shorting it pays.

Events: results/listings.csv (listings.py) — Upbit / Bithumb KRW markets and Binance spot USDT pairs opened
for a coin whose Binance USDT perp had already traded for 30+ days. Times are the venue's first candle hour.
Short entries on the Binance perp at the listing hour, an hour or four later, or the next 00:00 UTC; exits as in
STRATEGY.md (TP −15%, stop +15%, 48h) or wider (stop +30%, 7 days). 10% margin per trade.
"""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from farm_filter import fetch_ls
from short_study import H_DAY, load, pct

HERE = Path(__file__).resolve().parent
HALVES = {"2024-01..2025-06": ("2024-01-01", "2025-07-01"), "2025-07..2026-10": ("2025-07-01", "2026-11-01")}


def main() -> None:
    syms = load()
    ev = pd.read_csv(HERE / "results" / "listings.csv", parse_dates=["listed"])
    rows = []
    for e in ev.itertuples():
        s = syms.get(e.perp)
        if s is None:
            continue
        H = int(e.listed.timestamp() // 3600)
        k = H - s.t0
        if k < 73 or k + 169 >= s.n:
            continue
        p0 = s.open[k]
        rows.append({
            "venue": e.venue, "sym": e.perp, "listed": e.listed, "H": H,
            "pre24": s.close[k - 1] / s.close[k - 25] - 1, "pre72": s.close[k - 1] / s.close[k - 73] - 1,
            "r1h": s.close[k] / p0 - 1, "r4h": s.close[k + 3] / p0 - 1, "r24h": s.close[k + 23] / p0 - 1,
            "r48h": s.close[k + 47] / p0 - 1, "r7d": s.close[k + 167] / p0 - 1,
            "up48": s.high[k:k + 48].max() / p0 - 1, "dn48": s.low[k:k + 48].min() / p0 - 1,
        })
    d = pd.DataFrame(rows)
    d["half"] = np.where(d.listed < pd.Timestamp("2025-07-01", tz="UTC"), "2024-01..2025-06", "2025-07..2026-10")
    out = [f"## Listing pumps of existing perps ({len(d)} events, Binance perp prices)\n",
           "### What the perp did (median; positive = price went up, bad for a short)\n",
           "| venue | events | 24h before listing | 72h before | listing hour | +24h | +48h | +7d | max rise in 48h |",
           "|---|---|---|---|---|---|---|---|---|"]
    for v, x in [("all", d)] + list(d.groupby("venue")):
        out.append(f"| {v} | {len(x)} | {pct(x.pre24.median())} | {pct(x.pre72.median())} | {pct(x.r1h.median())} | "
                   f"{pct(x.r24h.median())} | {pct(x.r48h.median())} | {pct(x.r7d.median())} | {pct(x.up48.median())} |")
    out.append("\nMeans (the tail matters for a short): " + ", ".join(
        f"{v}: +48h {pct(x.r48h.mean())}, +7d {pct(x.r7d.mean())}" for v, x in [("all", d)] + list(d.groupby("venue"))))

    # short strategies
    ls = fetch_ls([(r.sym, int(r.H + off) * 3600) for r in d.itertuples() for off in (0, 1, 4)])
    out += ["", "### Shorting the perp (per trade on margin, 1x)\n",
            "| venue | entry | filter | exits | " + " | ".join(HALVES) + " |", "|---|---|---|---|" + "---|" * len(HALVES)]
    entries = {"listing hour": lambda r: r.H, "+1h": lambda r: r.H + 1, "+4h": lambda r: r.H + 4,
               "next 00:00 UTC": lambda r: (r.H // H_DAY + 1) * H_DAY}
    filters = {"none": lambda x: np.ones(len(x), bool),
               "pumped 20%+ in the 24h before": lambda x: (x.pre24 >= 0.20).to_numpy(),
               "ratio > 1.5 at entry": lambda x: (x.ls > 1.5).to_numpy()}
    exits = {"TP −15% / stop +15% / 48h": (0.15, 0.15, 2), "TP −15% / stop +30% / 7d": (0.15, 0.30, 7)}
    for venue, x0 in [("all", d)] + list(d.groupby("venue")):
        for ename, fn in entries.items():
            x = x0.copy()
            x["entry_h"] = [fn(r) for r in x.itertuples()]
            x["day"] = x.entry_h // H_DAY - 1
            x["ls"] = [ls.get((r.sym, int(r.entry_h) * 3600), np.nan) for r in x.itertuples()]
            x = x.sort_values("entry_h").reset_index(drop=True)
            P = g.Paths(syms, x, g.MAX_H)
            for fname, f in filters.items():
                for xname, (tp, stop, hold) in exits.items():
                    ret = P.outcome(tp, stop, hold)[0]
                    m = f(x) & ~np.isnan(ret)
                    cells = []
                    for hname in HALVES:
                        sel = m & (x.half == hname).to_numpy()
                        cells.append(f"{sel.sum()}, {pct(np.mean(ret[sel]))}, win {np.mean(ret[sel] > 0):.0%}"
                                     if sel.sum() >= 5 else f"{sel.sum()}, —")
                    out.append(f"| {venue} | {ename} | {fname} | {xname} | " + " | ".join(cells) + " |")
    (HERE / "results" / "listing_event.md").write_text("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    main()

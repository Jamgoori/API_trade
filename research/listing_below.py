"""Listing shorts when the price at entry is already below where it was: below the price before the announcement,
or below the price when trading opened. TP −15% / stop +15% / 48h."""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from listing_tp_runup import listing_frame
from short_study import load, pct

HERE = Path(__file__).resolve().parent
SPLIT = pd.Timestamp("2025-07-01").timestamp() // 3600


def table(title, x, ret, early, bands):
    out = [f"### {title}\n", "| price at entry vs then | trades | per trade | win | early / late |", "|---|---|---|---|---|"]
    for lo, hi, label in bands:
        b = (x >= lo) & (x < hi) & ~np.isnan(ret)
        out.append(f"| {label} | {b.sum()} | {pct(ret[b].mean())} | {np.mean(ret[b] > 0):.0%} | "
                   f"{pct(ret[b & early].mean())} / {pct(ret[b & ~early].mean())} |")
    return out + [""]


def main() -> None:
    syms = load()
    lst = listing_frame(syms)
    out = ["## Listing shorts when the price at entry is already lower\n"]
    for slip, ss in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP, g.STOP_SLIP = slip, ss
        ret = g.Paths(syms, lst, g.MAX_H).outcome(0.15, 0.15, 2)[0]
        early = (lst.entry_h < SPLIT).to_numpy()
        first = np.array([syms[r.sym].open[r.entry_h - syms[r.sym].t0] / syms[r.sym].open[r.entry_h - 1 - syms[r.sym].t0] - 1
                          for r in lst.itertuples()])
        out.append(f"## Slippage {slip:.1%}\n")
        out += table("vs 24 hours before entry (before the announcement)", lst.runup.to_numpy(), ret, early,
                     ((-9, -0.05, "5%+ lower"), (-0.05, 0.0, "0-5% lower"), (0.0, 9, "higher")))
        out += table("vs when trading opened (the first trading hour's move)", first, ret, early,
                     ((-9, -0.05, "5%+ lower"), (-0.05, 0.0, "0-5% lower"), (0.0, 0.05, "0-5% higher"), (0.05, 9, "5%+ higher")))
        # real announcement times (2025-2026)
        a = pd.read_csv(HERE / "results" / "krw_announcements.csv")
        a = a[a.announced.notna()].copy()
        a["listed"] = pd.to_datetime(a.listed, utc=True)
        a["ah"] = (pd.to_datetime(a.announced, utc=True) - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(hours=1)
        a = a[a.listed - pd.to_datetime(a.announced, utc=True) <= pd.Timedelta(hours=12)]
        key = {(r.perp, int((r.listed - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(hours=1)) + 1): int(r.ah) for r in a.itertuples()}
        ah = np.array([key.get((r.sym, r.entry_h), -1) for r in lst.itertuples()])
        has = ah >= 0
        vs_ann = np.full(len(lst), np.nan)
        for i, r in enumerate(lst.itertuples()):
            if has[i] and ah[i] - syms[r.sym].t0 >= 0:
                vs_ann[i] = syms[r.sym].open[r.entry_h - syms[r.sym].t0] / syms[r.sym].open[ah[i] - syms[r.sym].t0] - 1
        out += table("vs the announcement hour's open (real announcement times, 2025-2026)", np.nan_to_num(vs_ann, nan=99),
                     np.where(np.isnan(vs_ann), np.nan, ret), early,
                     ((-9, 0.0, "lower (the announcement pump fully faded)"), (0.0, 0.10, "0-10% higher"), (0.10, 50, "10%+ higher")))
        print("\n".join(out[-24:]), flush=True)
    g.SLIP, g.STOP_SLIP = 0.001, 0.005
    (HERE / "results" / "listing_below.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

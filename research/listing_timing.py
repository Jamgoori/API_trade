"""Listing shorts: enter after the announcement or after trading starts?

1. Binance spot listings: announcement times from Binance's announcement feed ("Binance Will List X (X)").
2. Upbit / Bithumb KRW listings: no announcement history is available, and KRW listings usually open 1-3 hours
   after the notice, so entries from 3 hours before to 2 hours after the first trading hour stand in for it.
Exits as now: TP −15% / stop +15% / 48h, slippage 0.3% (stop 1%).
"""

import re
from pathlib import Path

import numpy as np
import pandas as pd
import requests

import tp_grid as g
from short_study import load, pct

HERE = Path(__file__).resolve().parent
SPLIT = pd.Timestamp("2025-07-01", tz="UTC")


def binance_announcements() -> pd.DataFrame:
    rows = []
    for page in range(1, 40):
        r = requests.get("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query",
                         params={"type": 1, "catalogId": 48, "pageNo": page, "pageSize": 50}, timeout=20).json()
        arts = [a for c in (r.get("data") or {}).get("catalogs", []) for a in c.get("articles", [])]
        if not arts:
            break
        for a in arts:
            t = a.get("title", "")
            if not t.startswith("Binance Will List"):
                continue
            for base in re.findall(r"\(([A-Z0-9]{2,15})\)", t):
                rows.append({"base": base, "announced": pd.Timestamp(a["releaseDate"], unit="ms", tz="UTC"), "title": t})
    return pd.DataFrame(rows)


def outcome(syms, rows):
    """rows: DataFrame with sym, entry_h. Returns per-row return (TP 15 / stop 15 / 48h)."""
    d = rows[[s in syms and syms[s].t0 <= h < syms[s].t0 + syms[s].n - 48 for s, h in zip(rows.sym, rows.entry_h)]]
    d = d.reset_index(drop=True)
    if d.empty:
        return d, np.array([])
    d["day"] = d.entry_h // 24 - 1
    P = g.Paths(syms, d, g.MAX_H)
    return d, P.outcome(0.15, 0.15, 2)[0]


def main() -> None:
    syms = load()
    g.SLIP, g.STOP_SLIP = 0.003, 0.01
    ev = pd.read_csv(HERE / "results" / "listings.csv", parse_dates=["listed"])
    ev["listed"] = pd.to_datetime(ev.listed, utc=True)
    ev["h"] = (ev.listed - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(hours=1)
    out = ["## Listing shorts: after the announcement or after trading starts?\n"]

    # 1. Binance spot: exact announcement times
    ann = binance_announcements()
    bs = ev[ev.venue == "binance_spot"].copy()
    match = []
    for r in bs.itertuples():
        a = ann[(ann.base == r.base) & (ann.announced <= r.listed) & (ann.announced >= r.listed - pd.Timedelta(days=14))]
        if len(a):
            match.append((r.perp, int(r.h), a.announced.max(), r.listed))
    m = pd.DataFrame(match, columns=["sym", "h", "announced", "listed"])
    m["lag_h"] = (m.listed - m.announced) / pd.Timedelta(hours=1)
    m["ah"] = (m.announced - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(hours=1)
    out += [f"### Binance spot listings with an announcement found: {len(m)} of {len(bs)}; hours from announcement to "
            f"trading: median {m.lag_h.median():.0f}, range {m.lag_h.min():.0f}-{m.lag_h.max():.0f}\n",
            "| entry | trades | per trade | win |", "|---|---|---|---|"]
    for label, col, k in (("announcement +1h", "ah", 1), ("announcement +4h", "ah", 4),
                          ("trading start (listing hour)", "h", 0), ("trading start +1h (now)", "h", 1)):
        d, ret = outcome(syms, pd.DataFrame({"sym": m.sym, "entry_h": m[col].astype(int) + k}))
        ok = ~np.isnan(ret)
        out.append(f"| {label} | {ok.sum()} | {pct(ret[ok].mean()) if ok.any() else '—'} | {np.mean(ret[ok] > 0):.0%} |")
    out.append("")
    print("\n".join(out), flush=True)

    # 2. Upbit / Bithumb KRW: entry hours around the first trading hour
    out += ["### Upbit / Bithumb KRW listings: entry hour relative to the first trading hour (per trade, win; early / late half)\n",
            "| entry | upbit | bithumb |", "|---|---|---|"]
    for k in (-3, -2, -1, 0, 1, 2):
        cells = []
        for venue in ("upbit", "bithumb"):
            e = ev[ev.venue == venue]
            d, ret = outcome(syms, pd.DataFrame({"sym": e.perp, "entry_h": e.h.astype(int) + k}))
            ok = ~np.isnan(ret)
            t = pd.to_datetime(d.entry_h * 3600, unit="s", utc=True)
            early = (t < SPLIT).to_numpy()
            cells.append(f"{ok.sum()}: {pct(ret[ok].mean())}, {np.mean(ret[ok] > 0):.0%} "
                         f"({pct(ret[ok & early].mean())} / {pct(ret[ok & ~early].mean())})")
        label = f"{'+' if k >= 0 else '−'}{abs(k)}h" + (" (now)" if k == 1 else " (trading opens)" if k == 0 else "")
        out.append(f"| {label} | " + " | ".join(cells) + " |")
        print(out[-1], flush=True)
    (HERE / "results" / "listing_timing.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

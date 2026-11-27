"""Listing shorts: price in the first minutes of the entry hour, when a funding settlement falls on that hour.
Does waiting a few minutes after HH:00 give a better (higher) short price? 1m klines from data.binance.vision."""

from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from binance_data import KLINE_COLS, _fetch, _read_zip_csv
from funding_filter import funding_at_entry
from listing_tp_runup import listing_frame
from short_study import load, pct

MINS = (1, 2, 3, 5, 10, 15, 30)


def minute_bars(sym, day):
    p = _fetch(f"data/futures/um/daily/klines/{sym}/1m/{sym}-1m-{day}.zip")
    return None if p is None else _read_zip_csv(p, KLINE_COLS)


def main():
    syms = load()
    lst = listing_frame(syms)
    lst["day_s"] = pd.to_datetime(lst.entry_h * 3600, unit="s").dt.strftime("%Y-%m-%d")
    keys = sorted(set(zip(lst.sym, lst.day_s)))
    with ThreadPoolExecutor(32) as ex:
        got = dict(zip(keys, ex.map(lambda k: minute_bars(*k), keys)))
    bars = [got[(r.sym, r.day_s)] for r in lst.itertuples()]
    rows = []
    for r, k in zip(lst.itertuples(), bars):
        if k is None:
            continue
        t0 = r.entry_h * 3_600_000
        k = k.set_index("open_time")
        if t0 not in k.index:
            continue
        p0 = float(k.at[t0, "open"])
        low5 = float(k.loc[t0:t0 + 4 * 60_000, "low"].min())
        s = syms[r.sym]
        i = r.entry_h - s.t0
        settle = s.fund[i]                       # funding charged at the start of the entry hour (0 if none)
        rate, iv = funding_at_entry(s, r.entry_h)
        rows.append({"sym": r.sym, "settle": settle != 0, "rate4": (rate or 0) * 4 / (iv or 8), "low5": low5 / p0 - 1,
                     **{f"m{m}": float(k.at[t0 + m * 60_000, "open"]) / p0 - 1 for m in MINS if t0 + m * 60_000 in k.index}})
    d = pd.DataFrame(rows)
    groups = (("funding settles at entry, rate <= -0.3% per 4h", d.settle & (d.rate4 <= -0.003)),
              ("funding settles at entry, other rates", d.settle & (d.rate4 > -0.003)),
              ("no settlement at entry", ~d.settle))
    out = ["## Listing entry hour: price after HH:00 relative to the HH:00 open (higher is better for the short)\n",
           "| group | n | " + " | ".join(f"+{m} min" for m in MINS) + " | low in first 5 min |", "|---|---|" + "---|" * (len(MINS) + 1)]
    for label, b in groups:
        x = d[b]
        out.append(f"| {label} | {len(x)} | " + " | ".join(f"{pct(x[f'm{m}'].median())} ({np.mean(x[f'm{m}'] > 0):.0%} up)" for m in MINS)
                   + f" | {pct(x.low5.median())} |")
    print("\n".join(out))
    from pathlib import Path
    (Path(__file__).resolve().parent / "results" / "listing_minute.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

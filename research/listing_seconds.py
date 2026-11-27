"""Listing entries on a funding-settlement hour: second-by-second prices around HH:00:00 from aggTrades
(data.binance.vision). Reference = last trade before HH:00:00. Files are read once and deleted."""

import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from funding_filter import funding_at_entry
from listing_tp_runup import listing_frame
from short_study import load, pct

FILES = "https://data.binance.vision"
BUCKETS = ((-30, 0, "last 30s before"), (0, 2, "0-2s"), (2, 5, "2-5s"), (5, 10, "5-10s"), (10, 20, "10-20s"),
           (20, 30, "20-30s"), (30, 60, "30-60s"), (60, 120, "1-2 min"), (120, 300, "2-5 min"), (300, 600, "5-10 min"))


def window(sym, day, t0):
    url = f"{FILES}/data/futures/um/daily/aggTrades/{sym}/{sym}-aggTrades-{day}.zip"
    try:
        r = requests.get(url, timeout=120)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        raw = z.read(z.namelist()[0])
    first = raw.split(b"\n", 1)[0]
    df = pd.read_csv(io.BytesIO(raw), header=0 if b"price" in first else None, usecols=[1, 2, 5])
    df.columns = ["price", "qty", "ts"]
    df = df[(df.ts >= t0 - 60_000) & (df.ts < t0 + 600_000)]
    return df


def main():
    syms = load()
    lst = listing_frame(syms)
    rows = []
    for r in lst.itertuples():
        s = syms[r.sym]
        rate, iv = funding_at_entry(s, r.entry_h)
        rows.append((r.sym, r.entry_h, s.fund[r.entry_h - s.t0] != 0, (rate or 0) * 4 / (iv or 8)))
    d = pd.DataFrame(rows, columns=["sym", "h", "settle", "rate4"]).drop_duplicates(["sym", "h"])
    d = d[d.settle].reset_index(drop=True)
    d["day"] = pd.to_datetime(d.h * 3600, unit="s").dt.strftime("%Y-%m-%d")

    def one(r):
        t0 = int(r.h) * 3_600_000
        w = window(r.sym, r.day, t0)
        if w is None or w.empty:
            return None
        before = w[w.ts < t0]
        if before.empty:
            return None
        ref = float(before.price.iloc[-1])
        out = {"sym": r.sym, "rate4": r.rate4}
        for a, b, label in BUCKETS:
            x = w[(w.ts >= t0 + a * 1000) & (w.ts < t0 + b * 1000)]
            out[label] = (float((x.price * x.qty).sum() / x.qty.sum()) / ref - 1) if len(x) else np.nan
        x = w[(w.ts >= t0) & (w.ts < t0 + 30_000)]
        out["low 0-30s"] = float(x.price.min()) / ref - 1 if len(x) else np.nan
        return out

    with ThreadPoolExecutor(8) as ex:
        res = [x for x in ex.map(one, d.itertuples()) if x]
    res = pd.DataFrame(res)
    cols = [b[2] for b in BUCKETS] + ["low 0-30s"]
    out = ["## Listing entries on a settlement hour: volume-weighted price vs the last trade before HH:00:00 (median; share above)\n",
           "| funding per 4h | n | " + " | ".join(cols) + " |", "|---|---|" + "---|" * len(cols)]
    for label, b in (("<= -0.3%", res.rate4 <= -0.003), ("> -0.3%", res.rate4 > -0.003)):
        x = res[b]
        out.append(f"| {label} | {len(x)} | " + " | ".join(f"{pct(x[c].median())} ({np.mean(x[c] > 0):.0%})" for c in cols) + " |")
    print("\n".join(out))
    (Path(__file__).resolve().parent / "results" / "listing_seconds.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

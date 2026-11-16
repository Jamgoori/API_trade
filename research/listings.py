"""Listing times of coins that were already trading as Binance USDT perps (no TGE listings).

Sources: Upbit and Bithumb KRW markets (first candle = listing) through their public quotation APIs, and
Binance spot USDT pairs (first 1h kline in data.binance.vision). A listing counts when the coin's Binance
USDT perp had been trading for at least 30 days before it. Output: results/listings.csv
"""

from __future__ import annotations

import io
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from binance_data import _get, list_bucket
from short_study import H_DAY, load

HERE = Path(__file__).resolve().parent
START = pd.Timestamp("2023-12-01", tz="UTC")
S = requests.Session()


def krw_first_candle(base_url: str, market: str) -> pd.Timestamp | None:
    """Earliest daily candle of a KRW market (None if older than START)."""
    to = None
    oldest = None
    for _ in range(12):
        params = {"market": market, "count": 200}
        if to:
            params["to"] = to
        for attempt in range(5):
            r = S.get(f"{base_url}/v1/candles/days", params=params, timeout=20)
            if r.status_code == 200:
                break
            time.sleep(1 + attempt)
        rows = r.json() if r.status_code == 200 else []
        if not rows:
            break
        oldest = pd.Timestamp(rows[-1]["candle_date_time_utc"], tz="UTC")
        if len(rows) < 200:
            break
        if oldest < START:
            return None
        to = oldest.strftime("%Y-%m-%dT%H:%M:%SZ")
        time.sleep(0.12)
    return oldest


def krw_listing_hour(base_url: str, market: str, day: pd.Timestamp) -> pd.Timestamp:
    """First hourly candle on the listing day."""
    to = (day + pd.Timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = S.get(f"{base_url}/v1/candles/minutes/60", params={"market": market, "count": 24, "to": to}, timeout=20)
    rows = r.json() if r.status_code == 200 else []
    if not rows:
        return day
    return min(pd.Timestamp(x["candle_date_time_utc"], tz="UTC") for x in rows)


def krw_listings(venue: str, base_url: str) -> list[dict]:
    markets = [m["market"] for m in S.get(f"{base_url}/v1/market/all", timeout=20).json() if m["market"].startswith("KRW-")]
    out = []
    for m in markets:
        first = krw_first_candle(base_url, m)
        if first is None or first < START:
            continue
        hour = krw_listing_hour(base_url, m, first)
        out.append({"venue": venue, "base": m.split("-", 1)[1], "listed": hour})
        time.sleep(0.12)
    return out


def binance_spot_listings() -> list[dict]:
    prefixes, _ = list_bucket("data/spot/monthly/klines/")
    syms = [p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes]
    syms = [s for s in syms if s.endswith("USDT")]

    def one(sym):
        _, keys = list_bucket(f"data/spot/monthly/klines/{sym}/1h/", None)
        keys = sorted(k for k in keys if k.endswith(".zip"))
        if not keys:
            return None
        r = _get(f"https://data.binance.vision/{keys[0]}")
        if r.status_code != 200:
            return None
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            first = z.read(z.namelist()[0]).split(b"\n")[0].split(b",")
        if not first[0].isdigit():  # header row
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                first = z.read(z.namelist()[0]).split(b"\n")[1].split(b",")
        ts = int(first[0])
        ts = ts // 1000 if ts > 10 ** 14 else ts  # microsecond timestamps in newer files
        t = pd.Timestamp(ts, unit="ms", tz="UTC")
        return {"venue": "binance_spot", "base": sym[:-4], "listed": t} if t >= START else None

    with ThreadPoolExecutor(16) as ex:
        return [x for x in ex.map(one, syms) if x]


def main() -> None:
    syms = load()
    perp_start = {name: pd.Timestamp(s.t0 * 3600, unit="s", tz="UTC") for name, s in syms.items()}
    rows = binance_spot_listings()
    print(f"binance spot listings since {START.date()}: {len(rows)}", flush=True)
    for venue, url in (("upbit", "https://api.upbit.com"), ("bithumb", "https://api.bithumb.com")):
        got = krw_listings(venue, url)
        print(f"{venue} KRW listings since {START.date()}: {len(got)}", flush=True)
        rows += got
    df = pd.DataFrame(rows)
    out = []
    for r in df.itertuples():
        for perp in (f"{r.base}USDT", f"1000{r.base}USDT", f"1000000{r.base}USDT"):
            if perp in perp_start:
                age = (r.listed - perp_start[perp]).days
                out.append({"venue": r.venue, "base": r.base, "perp": perp, "listed": r.listed, "perp_age_days": age})
                break
    res = pd.DataFrame(out)
    res = res[res.perp_age_days >= 30].sort_values("listed")
    res.to_csv(HERE / "results" / "listings.csv", index=False)
    print(res.groupby("venue").size().to_string())


if __name__ == "__main__":
    main()

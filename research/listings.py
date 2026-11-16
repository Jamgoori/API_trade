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
ROOT_CACHE = HERE.parent / "data" / "listings"
START = pd.Timestamp("2023-12-01", tz="UTC")
S = requests.Session()


def _to_param(base_url: str, t_utc: pd.Timestamp) -> str:
    # Upbit takes an ISO UTC time; Bithumb takes "YYYY-MM-DD HH:MM:SS" in KST
    if "bithumb" in base_url:
        return (t_utc + pd.Timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")
    return t_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def _candles(base_url: str, path: str, params: dict) -> list:
    for attempt in range(5):
        try:
            r = S.get(f"{base_url}{path}", params=params, timeout=20)
        except requests.RequestException:
            time.sleep(1 + attempt)
            continue
        if r.status_code == 200:
            body = r.json()
            return body if isinstance(body, list) else []
        time.sleep(1 + attempt)
    return []


def krw_first_candle(base_url: str, market: str) -> pd.Timestamp | None:
    """Earliest daily candle of a KRW market (None if older than START)."""
    to = None
    oldest = None
    for _ in range(12):
        params = {"market": market, "count": 200}
        if to:
            params["to"] = to
        rows = _candles(base_url, "/v1/candles/days", params)
        if not rows:
            break
        oldest = pd.Timestamp(rows[-1]["candle_date_time_utc"], tz="UTC")
        if len(rows) < 200:
            break
        if oldest < START:
            return None
        to = _to_param(base_url, oldest)
        time.sleep(0.12)
    return oldest


def krw_listing_hour(base_url: str, market: str, day: pd.Timestamp) -> pd.Timestamp:
    """First hourly candle on the listing day."""
    rows = _candles(base_url, "/v1/candles/minutes/60",
                    {"market": market, "count": 24, "to": _to_param(base_url, day + pd.Timedelta(days=1))})
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
    cache = ROOT_CACHE
    cache.mkdir(parents=True, exist_ok=True)
    frames = []
    for venue, fetch in (("binance_spot", binance_spot_listings),
                         ("upbit", lambda: krw_listings("upbit", "https://api.upbit.com")),
                         ("bithumb", lambda: krw_listings("bithumb", "https://api.bithumb.com"))):
        path = cache / f"listings_{venue}.csv"
        if path.exists():
            f = pd.read_csv(path, parse_dates=["listed"])
        else:
            f = pd.DataFrame(fetch())
            f.to_csv(path, index=False)
        print(f"{venue} listings since {START.date()}: {len(f)}", flush=True)
        frames.append(f)
    df = pd.concat(frames, ignore_index=True)
    df["listed"] = pd.to_datetime(df["listed"], utc=True)
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

"""Download Binance USD-M futures history (1h klines + funding) from data.binance.vision.

The public bucket keeps files for delisted symbols too, so the universe is free of survivorship bias.
Raw zips are cached under data/binance/ (gitignored); reruns only fetch what is missing.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.etree import ElementTree

import numpy as np
import pandas as pd
import requests

BUCKET = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
FILES = "https://data.binance.vision"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
ROOT = Path(__file__).resolve().parent.parent / "data" / "binance"

# Old index perps that are no longer in exchangeInfo, plus coins that track something other than a crypto project.
NON_COIN = {"BTCDOMUSDT", "DEFIUSDT", "BLUEBIRDUSDT", "FOOTBALLUSDT", "USDCUSDT", "TUSDUSDT", "BUSDUSDT"}
EXINFO_URL = "https://www.binance.com/fapi/v1/exchangeInfo"  # fapi.binance.com is geo-blocked from some regions

_session = requests.Session()


def _get(url: str, **params) -> requests.Response:
    for attempt in range(5):
        try:
            r = _session.get(url, params=params or None, timeout=30)
            if r.status_code in (200, 404):
                return r
        except requests.RequestException:
            pass
        import time
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"failed: {url} {params}")


def list_bucket(prefix: str, delimiter: str | None = "/") -> tuple[list[str], list[str]]:
    """Return (common prefixes, keys) under prefix, following pagination."""
    prefixes, keys, marker = [], [], ""
    while True:
        params = {"prefix": prefix, "marker": marker}
        if delimiter:
            params["delimiter"] = delimiter
        root = ElementTree.fromstring(_get(BUCKET, **params).content)
        page_p = [e.text for e in root.findall("s3:CommonPrefixes/s3:Prefix", NS)]
        page_k = [e.text for e in root.findall("s3:Contents/s3:Key", NS)]
        prefixes += page_p
        keys += page_k
        if root.findtext("s3:IsTruncated", namespaces=NS) != "true":
            return prefixes, keys
        marker = root.findtext("s3:NextMarker", namespaces=NS) or max(page_p + page_k)


def exchange_info() -> dict[str, dict]:
    path = ROOT / "exchangeInfo.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_get(EXINFO_URL).content)
    import json
    return {s["symbol"]: s for s in json.loads(path.read_text())["symbols"]}


def is_coin(sym: str, info: dict[str, dict]) -> bool:
    """Crypto project perps only: drop equities, commodities, FX, pre-IPO, indices, RWA and stablecoins.

    Symbols missing from exchangeInfo were delisted before Binance listed TradFi perps, so they are coins.
    """
    if sym in NON_COIN:
        return False
    s = info.get(sym)
    if s is None:
        return True
    sub = s.get("underlyingSubType") or []
    return s.get("underlyingType") == "COIN" and "RWA" not in sub and "Stablecoin" not in sub


def usdt_perp_symbols() -> list[str]:
    prefixes, _ = list_bucket("data/futures/um/monthly/klines/")
    syms = [p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes]
    info = exchange_info()
    return sorted(s for s in syms if re.fullmatch(r"[A-Z0-9]+USDT", s) and is_coin(s, info))


def _month_ok(key: str, start: str, end: str) -> bool:
    m = re.search(r"(\d{4}-\d{2})\.zip$", key)
    return bool(m) and start <= m.group(1) <= end


def _fetch(key: str) -> Path | None:
    path = ROOT / "raw" / key.removeprefix("data/futures/um/monthly/").removeprefix("data/futures/um/")
    if path.exists():
        return path
    r = _get(f"{FILES}/{key}")
    if r.status_code != 200:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(r.content)
    tmp.rename(path)
    return path


def _read_zip_csv(path: Path, names: list[str]) -> pd.DataFrame:
    with zipfile.ZipFile(path) as z:
        raw = z.read(z.namelist()[0])
    first = raw.split(b"\n", 1)[0]
    header = 0 if re.search(rb"[a-z]", first) else None
    df = pd.read_csv(io.BytesIO(raw), header=header)
    df = df.iloc[:, : len(names)]
    df.columns = names
    return df


METRIC_COLS = ["create_time", "symbol", "oi", "oi_value", "top_ls_accounts", "top_ls_positions", "ls_accounts",
               "taker_ls_vol"]


def metrics(sym: str, day: str) -> pd.DataFrame | None:
    """5-minute open interest and long/short ratios for one UTC day (YYYY-MM-DD); None if Binance has no file."""
    path = _fetch(f"data/futures/um/daily/metrics/{sym}/{sym}-metrics-{day}.zip")
    if path is None:
        return None
    df = _read_zip_csv(path, METRIC_COLS)
    df["ts"] = (pd.to_datetime(df["create_time"]) - pd.Timestamp(0)) // pd.Timedelta(seconds=1)
    return df.drop(columns=["create_time", "symbol"]).sort_values("ts")


KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "count",
              "taker_buy_volume", "taker_buy_quote_volume"]
FUND_COLS = ["calc_time", "interval_h", "rate"]


def download_symbol(sym: str, start: str, end: str) -> str:
    out = ROOT / "panel" / f"{sym}.npz"
    if out.exists():
        return f"{sym}: cached"
    _, kkeys = list_bucket(f"data/futures/um/monthly/klines/{sym}/1h/", None)
    _, fkeys = list_bucket(f"data/futures/um/monthly/fundingRate/{sym}/", None)
    kpaths = [p for p in (_fetch(k) for k in kkeys if k.endswith(".zip") and _month_ok(k, start, end)) if p]
    fpaths = [p for p in (_fetch(k) for k in fkeys if k.endswith(".zip") and _month_ok(k, start, end)) if p]
    if not kpaths:
        return f"{sym}: no klines in range"
    k = pd.concat([_read_zip_csv(p, KLINE_COLS) for p in sorted(kpaths)])
    k = k.drop_duplicates("open_time").sort_values("open_time")
    t = (k["open_time"].to_numpy(np.int64) // 3_600_000).astype(np.int64)  # hour index since epoch
    if fpaths:
        f = pd.concat([_read_zip_csv(p, FUND_COLS) for p in sorted(fpaths)]).drop_duplicates("calc_time")
        ft = (f["calc_time"].to_numpy(np.int64) // 3_600_000).astype(np.int64)
        fr = f["rate"].to_numpy(float)
    else:
        ft, fr = np.zeros(0, np.int64), np.zeros(0)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out, t=t, open=k["open"].to_numpy(float), high=k["high"].to_numpy(float), low=k["low"].to_numpy(float),
        close=k["close"].to_numpy(float), qvol=k["quote_volume"].to_numpy(float),
        taker_buy_qvol=k["taker_buy_quote_volume"].to_numpy(float), fund_t=ft, fund_rate=fr)
    return f"{sym}: {len(t)} bars, {len(ft)} funding"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01")
    ap.add_argument("--end", default="2026-10")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--list-only", action="store_true")
    a = ap.parse_args()
    syms = usdt_perp_symbols()
    print(f"{len(syms)} USDT perp symbols", flush=True)
    if a.list_only:
        print(" ".join(syms))
        return
    with ThreadPoolExecutor(a.workers) as ex:
        for i, msg in enumerate(ex.map(lambda s: _safe(s, a.start, a.end), syms), 1):
            print(f"[{i}/{len(syms)}] {msg}", flush=True)


def _safe(sym: str, start: str, end: str) -> str:
    try:
        return download_symbol(sym, start, end)
    except Exception as e:  # keep going; report at the end
        return f"{sym}: ERROR {e!r}"


if __name__ == "__main__":
    sys.exit(main())

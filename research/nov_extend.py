"""Append November 2026 hourly bars (data.binance.vision daily files) to the panel symbols still trading on
2026-10-31, for replays of the live period. Funding is not in the daily files and is left at zero."""

from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from binance_data import KLINE_COLS, _fetch, _read_zip_csv
from short_study import load


def november(sym, days):
    parts = []
    for d in days:
        p = _fetch(f"data/futures/um/daily/klines/{sym}/1h/{sym}-1h-{d}.zip")
        if p is not None:
            parts.append(_read_zip_csv(p, KLINE_COLS))
    return sym, (pd.concat(parts).drop_duplicates("open_time").sort_values("open_time") if parts else None)


def extended(last_day: str):
    syms = load()
    end = max(s.t0 + s.n for s in syms.values())
    live = [k for k, s in syms.items() if s.t0 + s.n == end]
    days = [d.strftime("%Y-%m-%d") for d in pd.date_range("2026-11-01", last_day)]
    with ThreadPoolExecutor(32) as ex:
        got = dict(ex.map(lambda s: november(s, days), live))
    for name, k in got.items():
        if k is None or k.empty:
            continue
        s = syms[name]
        t = (k["open_time"].to_numpy(np.int64) // 3_600_000) - s.t0
        n = int(t[-1]) + 1
        def grow(a, vals, fill_prev=True):
            b = np.full(n, np.nan)
            b[: len(a)] = a
            b[t] = vals
            return b
        close = pd.Series(grow(s.close, k["close"].to_numpy(float))).ffill().to_numpy()
        def px(a, col):
            b = grow(a, k[col].to_numpy(float))
            return np.where(np.isnan(b), close, b)
        qv = np.zeros(n); qv[: s.n] = s.qvol; qv[t] = k["quote_volume"].to_numpy(float)
        fund = np.zeros(n); fund[: s.n] = s.fund
        s.open, s.high, s.low, s.close, s.qvol, s.fund = px(s.open, "open"), px(s.high, "high"), px(s.low, "low"), close, qv, fund
    end = max(s.t0 + s.n for s in syms.values())
    for s in syms.values():
        s.delisted = s.t0 + s.n < end - 48
    return syms

"""Grid search for the big-up-day short with a fixed take-profit, validated across two periods.

Signal: a coin outside the top 100 by volume closes a UTC day >= trigger above the previous close.
Entry: short at the next 00:00 open, 1x isolated, one position per coin at a time.
Exit, whichever comes first: take-profit (price falls tp below entry), stop (price rises stop above entry),
liquidation (only through a gap), or the end of the holding period.
Inside one 1h bar that touches both the stop and the take-profit, the stop is assumed to fill first.

Each combination is scored in 2022-2024 and in 2025-01..2026-10 with the same 50-slot portfolio as
pump_day_short.py. A combination is only worth anything if it holds up in both periods.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import numpy as np
import pandas as pd

from farm_filter import fetch_ls
from pump_day_short import SLOTS, signals
from short_study import FEE, H_DAY, MMR, SLIP, STOP_SLIP, load, pct, volume_ranks

MAKER = 0.0002   # take-profit rests as a limit order
MAX_H = 30 * H_DAY

TRIGGERS = (0.10, 0.20, 0.30)
TPS = (0.10, 0.15, 0.20, 0.30, 0.40, 0.50)
STOPS = (0.15, 0.20, 0.30, 0.50)
HOLDS = (3, 7, 14, 30)
BANS = (False, True)
RATIO = (None, 1.5)
MIN_TRADES = 100


class Paths:
    """Price path of every signal over the longest holding period, relative to the entry price.

    By default a signal on `day` enters at the next 00:00 open. Optional columns in `sig` change that:
    entry_h (absolute hour of the entry bar), entry_px (fill price inside that bar instead of its open) and
    partial (the entry happens inside the bar: its earlier low cannot take profit, its high can still stop out).
    """

    def __init__(self, syms, sig: pd.DataFrame, max_h: int = MAX_H):
        n = len(sig)
        self.max_h = max_h
        self.O = np.full((n, max_h), np.nan, np.float32)
        self.H = np.full((n, max_h), np.nan, np.float32)
        self.L = np.full((n, max_h), np.nan, np.float32)
        self.C = np.full((n, max_h), np.nan, np.float32)
        self.FC = np.zeros((n, max_h), np.float32)
        self.valid = np.zeros(n, np.int32)
        self.delisted = np.zeros(n, bool)
        self.entry_h = np.zeros(n, np.int64)
        has_h, has_px, has_part = "entry_h" in sig, "entry_px" in sig, "partial" in sig
        for r, e in enumerate(sig.itertuples()):
            s = syms[e.sym]
            i = (e.entry_h if has_h else (e.day + 1) * H_DAY) - s.t0
            m = max(0, min(max_h, s.n - i))
            self.valid[r], self.delisted[r], self.entry_h[r] = m, s.delisted, s.t0 + i
            if m == 0:
                continue
            px = e.entry_px if has_px and not pd.isna(e.entry_px) else s.open[i]
            p0 = px * (1 - SLIP)
            sl = slice(i, i + m)
            self.O[r, :m] = s.open[sl] / p0
            self.H[r, :m] = s.high[sl] / p0
            self.L[r, :m] = s.low[sl] / p0
            self.C[r, :m] = s.close[sl] / p0
            if has_part and e.partial is True:
                self.O[r, 0] = 1.0
                self.L[r, 0] = np.nan
            f = s.fund[sl] * s.open[sl] / p0
            f[0] = 0.0
            self.FC[r, :m] = np.cumsum(f)
        # hours from the entry bar to the last bar of each UTC day, for the daily mark to market
        self.off0 = (H_DAY - 1 - self.entry_h % H_DAY).astype(np.int64)
        self._first = {}

    def first(self, kind: str, level: float) -> np.ndarray:
        """First bar where the take-profit / stop level is touched (max_h if never)."""
        key = (kind, level)
        if key not in self._first:
            with np.errstate(invalid="ignore"):
                hit = self.L <= 1 - level if kind == "tp" else self.H >= 1 + level
            j = np.argmax(hit, axis=1)
            j[~hit.any(axis=1)] = self.max_h
            self._first[key] = j
        return self._first[key]

    def outcome(self, tp: float, stop: float, hold: int | None, lev: float = 1.0, truncate: bool = False):
        """Return on margin, exit bar and exit reason code for every signal (nan where the data runs out).

        hold None: no time limit, only take-profit / stop / liquidation end a trade (capped at max_h bars).
        truncate: a trade still open when the data ends is valued at the last bar instead of being dropped.
        """
        hh = self.max_h if hold is None else hold * H_DAY
        n = len(self.valid)
        rows = np.arange(n)
        end = np.minimum(hh, self.valid)
        ok = (self.valid >= hh) | (self.delisted & (self.valid >= 2)) | (truncate & (self.valid >= 2))
        jt = self.first("tp", tp)
        if np.ndim(stop):  # one stop level per signal (e.g. volatility-based)
            stop = np.asarray(stop, dtype=float)
            with np.errstate(invalid="ignore"):
                hit = self.H >= (1 + stop)[:, None]
            js = np.argmax(hit, axis=1)
            js[~hit.any(axis=1)] = self.max_h
        else:
            js = self.first("stop", stop)
        js_in = js < end
        jt_in = (jt < end) & (jt < js)  # a tie goes to the stop
        j = np.where(js_in & ~((jt < js) & jt_in), js, np.where(jt_in, jt, end - 1))
        j = np.clip(j, 0, self.max_h - 1)
        o, fc = self.O[rows, j], self.FC[rows, j]
        liqx = (1 / lev + 1 + fc - FEE) / (1 + MMR)
        ret = np.full(n, np.nan)
        why = np.zeros(n, np.int8)  # 0 time, 1 take-profit, 2 stop, 3 liquidated
        stop_now = js_in & ~jt_in
        x_stop = np.maximum(1 + stop, o) * (1 + STOP_SLIP)
        liq = stop_now & ((o >= liqx) | (x_stop >= liqx))
        ret = np.where(stop_now, lev * ((1 - x_stop) + fc - FEE - FEE * x_stop), ret)
        why = np.where(stop_now, 2, why)
        x_tp = np.minimum(1 - tp, o)
        ret = np.where(jt_in, lev * ((1 - x_tp) + fc - FEE - MAKER * x_tp), ret)
        why = np.where(jt_in, 1, why)
        timed = ~stop_now & ~jt_in
        x_end = self.C[rows, j] * (1 + SLIP)
        ret = np.where(timed, np.maximum(-1.0, lev * ((1 - x_end) + fc - FEE - FEE * x_end)), ret)
        ret = np.where(liq, -1.0, ret)
        why = np.where(liq, 3, why)
        ret = np.where(ok, ret, np.nan)
        # mark to market at each daily close before the exit day
        k = self.off0[:, None] + np.arange(hh // H_DAY + 1)[None, :] * H_DAY
        k = np.minimum(k, self.max_h - 1)
        mtm = np.maximum(-1.0, lev * ((1 - self.C[rows[:, None], k]) + self.FC[rows[:, None], k] - FEE))
        return ret, j, why, mtm


def run_combo(sig: pd.DataFrame, paths: Paths, mask: np.ndarray, tp, stop, hold, ban, day_a, day_b,
              margin_frac: float | None = None, lev: float = 1.0, truncate: bool = False, return_curve: bool = False):
    """margin_frac: margin per position as a share of equity (default 1/SLOTS); at most 1/margin_frac open."""
    ret, j, why, mtm = paths.outcome(tp, stop, hold, lev, truncate)
    frac = margin_frac or 1 / SLOTS
    slots = int(round(1 / frac))
    order = np.flatnonzero(mask)
    taken = []
    state: dict[str, tuple[int, bool]] = {}
    for r in order:  # sig is sorted by day
        if np.isnan(ret[r]):
            continue
        sym = sig.sym.iat[r]
        free_from, banned = state.get(sym, (-1, False))
        if banned or paths.entry_h[r] <= free_from:
            continue
        taken.append(r)
        state[sym] = (paths.entry_h[r] + j[r], ban and why[r] >= 2)
    if not taken:
        return None
    taken = np.array(taken)
    entry_day = paths.entry_h[taken] // H_DAY
    # portfolio with SLOTS equal slots, equity marked to market daily
    by_day: dict[int, list[int]] = {}
    for t, d in zip(taken, entry_day):
        by_day.setdefault(int(d), []).append(int(t))
    rng = np.random.default_rng(0)
    cash, open_, curve, skipped = 1.0, [], [], 0
    for day in range(day_a, day_b + 1):
        keep = []
        for p in open_:
            if p[2] < day:
                cash += p[0] * (1 + ret[p[1]])
            else:
                keep.append(p)
        open_ = keep

        def val(p, d):
            if p[2] <= d:
                return ret[p[1]]
            k = d - p[3]
            return mtm[p[1], k] if 0 <= k < mtm.shape[1] else 0.0

        equity = cash + sum(p[0] * (1 + val(p, day - 1)) for p in open_)
        todays = by_day.get(day, [])
        rng.shuffle(todays)
        for t in todays:
            if len(open_) >= slots:
                skipped += 1
                continue
            m = equity * frac
            cash -= m
            open_.append((m, t, int((paths.entry_h[t] + j[t]) // H_DAY), int(paths.entry_h[t] // H_DAY)))
        curve.append(cash + sum(p[0] * (1 + val(p, day)) for p in open_))
    eq = np.array(curve)
    years = len(eq) / 365
    final = eq[-1]
    cagr = final ** (1 / years) - 1 if final > 0 else -1.0
    maxdd = float((eq / np.maximum.accumulate(eq) - 1).min())
    r = ret[taken]
    w = why[taken]
    return {"trades": len(taken), "mean": float(np.mean(r)), "median": float(np.median(r)),
            "win": float(np.mean(r > 0)), "tp_rate": float(np.mean(w == 1)), "stop_rate": float(np.mean(w >= 2)),
            "cagr": cagr, "maxdd": maxdd, "calmar": cagr / abs(maxdd) if maxdd < 0 else np.nan, "skipped": skipped,
            **({"curve": eq} if return_curve else {})}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", nargs=3, action="append", metavar=("NAME", "START", "END"),
                    help="scoring period; give two (default 2022-24 and 2025-26)")
    ap.add_argument("--out", default="tp_grid.csv", help="file name under results/")
    args = ap.parse_args()
    out_dir = Path(__file__).resolve().parent / "results"
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    periods = ({n: (a, b) for n, a, b in args.period} if args.period
               else {"2022-24": ("2022-01-01", "2024-12-31"), "2025-26": ("2025-01-01", "2026-10-31")})
    rows = []
    for pname, (a, b) in periods.items():
        day_a = int(pd.Timestamp(a).timestamp() // 86400)
        day_b = min(int(pd.Timestamp(b).timestamp() // 86400), last)
        for trig in TRIGGERS:
            sig = signals(syms, ranks, trig, day_a, day_b).sort_values("day").reset_index(drop=True)
            ls = fetch_ls([(r.sym, (r.day + 1) * 86400) for r in sig.itertuples()])
            sig["ls"] = [ls.get((r.sym, (r.day + 1) * 86400), np.nan) for r in sig.itertuples()]
            paths = Paths(syms, sig)
            print(f"{pname} trigger +{trig:.0%}: {len(sig)} signals", flush=True)
            for ratio, tp, stop, hold, ban in itertools.product(RATIO, TPS, STOPS, HOLDS, BANS):
                mask = np.ones(len(sig), bool) if ratio is None else (sig.ls > ratio).to_numpy()
                res = run_combo(sig, paths, mask, tp, stop, hold, ban, day_a, day_b)
                if res is None:
                    continue
                rows.append({"period": pname, "trigger": trig, "ratio": ratio or 0, "tp": tp, "stop": stop,
                             "hold": hold, "ban": ban, **res})
            del paths
    df = pd.DataFrame(rows)
    df.to_csv(out_dir / args.out, index=False)
    print(f"{len(df)} rows written", flush=True)


if __name__ == "__main__":
    main()

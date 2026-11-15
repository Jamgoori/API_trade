"""Short small caps only on days they rose a lot, instead of on a fixed schedule.

Signal: a coin outside the top 100 by volume closes a UTC day at least X% above the previous day's close.
Entry: short at the next day's 00:00 open, 1x isolated, one position per coin at a time.

Prints per-trade statistics and a portfolio run where capital is split into fixed slots (new signals are
skipped while every slot is taken) and equity is marked to market at every daily close.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from farm_filter import fetch_ls
from short_study import FEE, H_DAY, SLIP, load, pct, sim_short, volume_ranks

SLOTS = 50  # 2% of equity as margin per position


def daily_close_index(s, day: int) -> int | None:
    k = (day + 1) * H_DAY - 1 - s.t0
    return k if 0 <= k < s.n else None


def signals(syms, ranks, thr: float, day_a: int, day_b: int, min_rank: int = 100) -> pd.DataFrame:
    """min_rank: only coins ranked below this by trailing 30-day volume (100 = outside the top 100)."""
    rows = []
    for name, s in syms.items():
        for day in range(max(day_a, s.t0 // H_DAY + 2), day_b):
            k, k0 = daily_close_index(s, day), daily_close_index(s, day - 1)
            if k is None or k0 is None:
                continue
            r = s.close[k] / s.close[k0] - 1
            if r < thr or day not in ranks.index or not ranks.at[day, name] > min_rank:
                continue
            rows.append({"sym": name, "day": day, "day_ret": r})
    return pd.DataFrame(rows)


def trades(syms, sig: pd.DataFrame, hold_days: int, stop, ban: bool = False) -> pd.DataFrame:
    """ban: once a short on a coin is stopped out or liquidated, never short that coin again."""
    out = []
    for name, g in sig.sort_values("day").groupby("sym"):
        s = syms[name]
        free_from = -1
        banned = False
        for e in g.itertuples():
            i = (e.day + 1) * H_DAY - s.t0
            if banned or i <= free_from or i >= s.n:
                continue
            res = sim_short(s, i, hold_days * H_DAY, 1.0, stop)
            if res is None:
                continue
            ret, why, j = res
            free_from = j
            banned = ban and why in ("stop", "liq")
            # mark to market at each daily close while open (return on margin, floored at a total loss)
            p0 = s.open[i] * (1 - SLIP)
            fc = np.cumsum(np.r_[0.0, s.fund[i + 1:j + 1] * s.open[i + 1:j + 1] / p0])
            ks = np.arange(i, j)
            ks = ks[(s.t0 + ks) % H_DAY == H_DAY - 1]
            u = np.maximum(-1.0, (1 - s.close[ks] / p0) + fc[ks - i] - FEE)
            mtm = dict(zip(((s.t0 + ks) // H_DAY).tolist(), u.tolist()))
            out.append({"sym": name, "day": e.day, "day_ret": e.day_ret, "entry_day": e.day + 1,
                        "exit_day": (s.t0 + j) // H_DAY, "ret": ret, "why": why, "mtm": mtm})
    return pd.DataFrame(out)


def portfolio(t: pd.DataFrame, day_a: int, day_b: int, slots: int = SLOTS) -> dict:
    by_entry = {d: g for d, g in t.groupby("entry_day")}
    cash, open_, curve, skipped, used = 1.0, [], [], 0, []
    rng = np.random.default_rng(0)
    for day in range(day_a, day_b + 1):
        # positions that closed during this day are realized at its end
        still = []
        for p in open_:
            if p["exit_day"] < day:
                cash += p["margin"] * (1 + p["ret"])
            else:
                still.append(p)
        open_ = still
        equity = cash + sum(p["margin"] * (1 + p["mtm"].get(day - 1, 0.0)) for p in open_)
        if day in by_entry:
            g = by_entry[day].sample(frac=1, random_state=int(rng.integers(1 << 31)))
            for e in g.itertuples():
                if len(open_) >= slots:
                    skipped += 1
                    continue
                margin = equity / slots
                cash -= margin
                open_.append({"margin": margin, "ret": e.ret, "exit_day": e.exit_day, "mtm": e.mtm})
        used.append(len(open_))
        curve.append(cash + sum(p["margin"] * (1 + (p["ret"] if p["exit_day"] <= day else p["mtm"].get(day, 0.0)))
                                for p in open_))
    eq = pd.Series(curve)
    years = len(eq) / 365
    return {"cagr": eq.iloc[-1] ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1.0,
            "maxdd": (eq / eq.cummax() - 1).min(), "final": eq.iloc[-1],
            "avg_open": np.mean(used), "skipped": skipped, "curve": eq}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2026-10-31")
    ap.add_argument("--save", type=Path, default=Path(__file__).resolve().parent / "results")
    ap.add_argument("--ban", action="store_true", help="never short a coin again after a stop or liquidation on it")
    a = ap.parse_args()
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    day_a = int(pd.Timestamp(a.start).timestamp() // 86400)
    day_b = min(int(pd.Timestamp(a.end).timestamp() // 86400), last)

    lines = [f"## Short only after a big up day: volume rank 101+, 1x isolated, {a.start} .. {a.end}"
             + (", coin dropped for good after a stop or liquidation" if a.ban else "") + "\n",
             "Signal: daily close (UTC) at least X% above the previous close. Entry at the next 00:00 open.",
             f"Portfolio: {SLOTS} equal slots (2% margin each), new signals skipped while all slots are taken, "
             "equity marked to market at every daily close.\n",
             "| day gain | ratio filter | hold | stop | trades | mean | median | win | liquidated | stopped | portfolio CAGR | max DD | avg open | skipped | coins dropped |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    print("\n".join(lines), flush=True)
    for thr in (0.10, 0.20, 0.30):
        sig = signals(syms, ranks, thr, day_a, day_b)
        ls = fetch_ls([(r.sym, (r.day + 1) * 86400) for r in sig.itertuples()])
        sig["ls"] = [ls.get((r.sym, (r.day + 1) * 86400), np.nan) for r in sig.itertuples()]
        for flabel, fmask in [("—", sig.ls.notna() | sig.ls.isna()), ("> 1.5 (crowd long)", sig.ls > 1.5),
                              ("< 0.7 (crowd short)", sig.ls < 0.7)]:
            for hold in (7, 30):
                for stop in (None, 0.5, 0.3):
                    if flabel != "—" and stop == 0.5:
                        continue
                    t = trades(syms, sig[fmask], hold, stop, ban=a.ban)
                    if t.empty:
                        continue
                    p = portfolio(t, day_a, day_b)
                    row = (f"| +{thr:.0%} | {flabel} | {hold}d | {'none' if stop is None else f'+{stop:.0%}'} | {len(t)} "
                           f"| {pct(t.ret.mean())} | {pct(t.ret.median())} | {(t.ret > 0).mean():.0%} "
                           f"| {(t.why == 'liq').mean():.1%} | {(t.why == 'stop').mean():.0%} "
                           f"| {pct(p['cagr'], 0)} | {pct(p['maxdd'], 0)} | {p['avg_open']:.0f} | {p['skipped']} "
                           f"| {t[t.why.isin(['stop', 'liq'])].sym.nunique()} |")
                    lines.append(row)
                    print(row, flush=True)
    a.save.mkdir(parents=True, exist_ok=True)
    suffix = "_ban" if a.ban else ""
    (a.save / f"pump_day_short_{a.start[:4]}_{a.end[:4]}{suffix}.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()

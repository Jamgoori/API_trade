"""Enter as soon as a coin is up 20% instead of waiting for the daily close.

Same rule as combination A (take-profit −15%, stop +15%, 3-day limit, drop a coin after a stop, optional
long/short ratio > 1.5), with four ways to time the entry, 2024-01..2026-10:

- daily close : the day closed >= +20% above the previous close; short at the next 00:00 open (the original)
- hourly      : an hourly close is >= +20% above the previous day's close; short at the next hour's open
- instant     : the price touches +20% above the previous day's close; short right there (inside the bar,
                the rest of that bar's high can stop the trade out but its earlier low cannot take profit)
- rolling 24h : an hourly close is >= +20% above the close 24 hours earlier; short at the next hour's open

The long/short ratio is the last 5-minute snapshot before the entry.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from farm_filter import fetch_ls
from pump_day_short import signals as daily_signals
from short_study import H_DAY, load, pct, volume_ranks

TRIG = 0.20
HERE = Path(__file__).resolve().parent


def intraday_signals(syms, ranks, day_a: int, day_b: int, mode: str) -> pd.DataFrame:
    rows = []
    for name, s in syms.items():
        for day in range(max(day_a, s.t0 // H_DAY + 2), day_b + 1):
            if day not in ranks.index or not ranks.at[day, name] > 100:
                continue
            a, b = day * H_DAY - s.t0, (day + 1) * H_DAY - s.t0
            if a < 25 or b > s.n:
                continue
            ref = s.close[a - 1]
            if mode == "rolling 24h":
                hit = np.flatnonzero(s.close[a:b] >= (1 + TRIG) * s.close[a - 24:b - 24])
            elif mode == "hourly":
                hit = np.flatnonzero(s.close[a:b] >= (1 + TRIG) * ref)
            else:
                hit = np.flatnonzero(s.high[a:b] >= (1 + TRIG) * ref)
            if not len(hit):
                continue
            h = a + int(hit[0])
            if mode == "instant":
                rows.append({"sym": name, "entry_h": s.t0 + h, "entry_px": max((1 + TRIG) * ref, s.open[h]),
                             "partial": True})
            elif h + 1 < s.n:
                rows.append({"sym": name, "entry_h": s.t0 + h + 1})
    df = pd.DataFrame(rows)
    df["day"] = df.entry_h // H_DAY - 1
    return df


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    periods = {"2024-01..2025-06": ("2024-01-01", "2025-06-30"), "2025-07..2026-10": ("2025-07-01", "2026-10-31"),
               "2024-01..2026-10": ("2024-01-01", "2026-10-31")}
    out = ["## Entry timing for combination A (TP −15%, stop +15%, 3 days, drop after stop, 1x)\n",
           "| entry timing | ratio filter | period | trades | per trade | win | 2% margin: CAGR / max DD | 15% margin: $10k → / max DD |",
           "|---|---|---|---|---|---|---|---|"]
    for mode in ("daily close", "hourly", "instant", "rolling 24h"):
        for pname, (a, b) in periods.items():
            da = int(pd.Timestamp(a).timestamp() // 86400)
            db = min(int(pd.Timestamp(b).timestamp() // 86400), last)
            if mode == "daily close":
                sig = daily_signals(syms, ranks, TRIG, da, db)
                sig["entry_h"] = (sig.day + 1) * H_DAY
            else:
                sig = intraday_signals(syms, ranks, da, db, mode)
            sig = sig.sort_values("entry_h").reset_index(drop=True)
            ls = fetch_ls([(r.sym, int(r.entry_h) * 3600) for r in sig.itertuples()])
            sig["ls"] = [ls.get((r.sym, int(r.entry_h) * 3600), np.nan) for r in sig.itertuples()]
            paths = g.Paths(syms, sig, g.MAX_H)
            for ratio in (1.5, None):
                mask = np.ones(len(sig), bool) if ratio is None else (sig.ls > ratio).to_numpy()
                r2 = g.run_combo(sig, paths, mask, 0.15, 0.15, 3, True, da, db, margin_frac=0.02)
                r15 = g.run_combo(sig, paths, mask, 0.15, 0.15, 3, True, da, db, margin_frac=0.15, return_curve=True)
                out.append(f"| {mode} | {'> 1.5' if ratio else 'none'} | {pname} | {r2['trades']} | {pct(r2['mean'])} "
                           f"| {r2['win']:.0%} | {pct(r2['cagr'], 0)} / {pct(r2['maxdd'], 0)} "
                           f"| ${r15['curve'][-1] * 10_000:,.0f} / {pct(r15['maxdd'], 0)} |")
                print(out[-1], flush=True)
    (HERE / "results" / "realtime_entry.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

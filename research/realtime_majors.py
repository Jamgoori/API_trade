"""Daily-close entry versus real-time detection with an hourly entry, each with and without the majors filter.

Rule A exits (take-profit −15%, stop +15%, 3 days, drop after stop, long/short ratio > 1.5, 1x).
Majors filter: skip when the BTC/ETH/SOL average move is above +7% or below −3%. For the daily-close entry the
move is the full UTC day; for the hourly entry it is the move from the previous day's close to the signal hour,
which is all that is known at that moment.
"""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from farm_filter import fetch_ls
from majors_filter import MAJORS, majors_day_return
from pump_day_short import signals as daily_signals
from realtime_entry import intraday_signals
from short_study import H_DAY, load, pct, volume_ranks

HERE = Path(__file__).resolve().parent
PERIODS = {"2024-01..2025-06": ("2024-01-01", "2025-06-30"), "2025-07..2026-10": ("2025-07-01", "2026-10-31"),
           "2024-01..2026-10": ("2024-01-01", "2026-10-31")}


def majors_so_far(syms, abs_h: int) -> float:
    """Average BTC/ETH/SOL move from the previous UTC day's close to the close of hour abs_h."""
    day = abs_h // H_DAY
    rets = []
    for m in MAJORS:
        s = syms[m]
        k, k0 = abs_h - s.t0, day * H_DAY - 1 - s.t0
        if 0 <= k0 and k < s.n:
            rets.append(s.close[k] / s.close[k0] - 1)
    return float(np.mean(rets)) if rets else np.nan


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    variants = [("daily close +20% → next 00:00 UTC", "daily", 0.20),
                ("hourly close +20% → next hour", "hourly", 0.20),
                ("hourly close +30% → next hour", "hourly", 0.30)]
    out = ["## Entry timing × majors filter (rule A exits, ratio > 1.5, 1x)\n",
           "Per cell: trades, average return per trade, $10k at 15% margin per trade (max drawdown).\n",
           "| entry | majors filter | " + " | ".join(PERIODS) + " |", "|---|---|" + "---|" * len(PERIODS)]
    for label, mode, trig in variants:
        rows = {False: [], True: []}
        for pname, (a, b) in PERIODS.items():
            da = int(pd.Timestamp(a).timestamp() // 86400)
            db = min(int(pd.Timestamp(b).timestamp() // 86400), last)
            if mode == "daily":
                sig = daily_signals(syms, ranks, trig, da, db)
                sig["entry_h"] = (sig.day + 1) * H_DAY
                sig["majors"] = [majors_day_return(syms, d) for d in sig.day]
            else:
                sig = intraday_signals(syms, ranks, da, db, mode, trig)
                sig["majors"] = [majors_so_far(syms, int(h) - 1) for h in sig.entry_h]
            sig = sig.sort_values("entry_h").reset_index(drop=True)
            ls = fetch_ls([(r.sym, int(r.entry_h) * 3600) for r in sig.itertuples()])
            sig["ls"] = [ls.get((r.sym, int(r.entry_h) * 3600), np.nan) for r in sig.itertuples()]
            P = g.Paths(syms, sig, g.MAX_H)
            for filt in (False, True):
                m = (sig.ls > 1.5).to_numpy().copy()
                if filt:
                    m &= ((sig.majors <= 0.07) & (sig.majors >= -0.03)).to_numpy()
                r = g.run_combo(sig, P, m, 0.15, 0.15, 3, True, da, db, margin_frac=0.15, return_curve=True)
                rows[filt].append(f"{r['trades']}, {pct(r['mean'])}, ${r['curve'][-1] * 10_000:,.0f} ({pct(r['maxdd'], 0)})")
        for filt in (False, True):
            out.append(f"| {label} | {'skip > +7% or < −3%' if filt else 'none'} | " + " | ".join(rows[filt]) + " |")
            print(out[-1], flush=True)
    (HERE / "results" / "realtime_majors.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

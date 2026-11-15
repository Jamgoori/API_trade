"""Combination A, skipping signals on days when the majors (BTC, ETH, SOL) rose.

Majors' day = average of the three coins' UTC daily close-to-close returns on the signal day.
Rule: +20% daily close, long/short ratio > 1.5, take-profit −15%, stop +15%, 3 days, drop after stop, 1x.
"""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from farm_filter import fetch_ls
from pump_day_short import signals
from short_study import H_DAY, load, pct, volume_ranks

HERE = Path(__file__).resolve().parent
PERIODS = {"2024-01..2025-06": ("2024-01-01", "2025-06-30"), "2025-07..2026-10": ("2025-07-01", "2026-10-31"),
           "2024-01..2026-10": ("2024-01-01", "2026-10-31")}
MAJORS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def majors_day_return(syms, day: int) -> float:
    rets = []
    for m in MAJORS:
        s = syms[m]
        k, k0 = (day + 1) * H_DAY - 1 - s.t0, day * H_DAY - 1 - s.t0
        if 0 <= k0 and k < s.n:
            rets.append(s.close[k] / s.close[k0] - 1)
    return float(np.mean(rets)) if rets else np.nan


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    rules = [
        ("all signals (current rule)", lambda x: np.ones(len(x), bool)),
        ("skip if majors > +0%", lambda x: x <= 0.0),
        ("skip if majors > +1%", lambda x: x <= 0.01),
        ("skip if majors > +2%", lambda x: x <= 0.02),
        ("skip if majors > +3%", lambda x: x <= 0.03),
        ("skip if majors moved more than ±3% (picked after seeing the table below)", lambda x: np.abs(x) <= 0.03),
        ("skip if majors > +5%", lambda x: x <= 0.05),
        ("skip if majors > +7%", lambda x: x <= 0.07),
        ("skip if majors > +7% or < −3% (picked after seeing the buckets)", lambda x: (x <= 0.07) & (x >= -0.03)),
        ("only majors > +0% (reverse)", lambda x: x > 0.0),
        ("only majors > +2% (reverse)", lambda x: x > 0.02),
    ]
    out = ["## Combination A with a majors filter (BTC/ETH/SOL average day return on the signal day)\n",
           "Per cell: trades, average return per trade, $10k at 15% margin per trade (max drawdown).\n",
           "| rule | " + " | ".join(PERIODS) + " |", "|---|" + "---|" * len(PERIODS)]
    data = {}
    for pname, (a, b) in PERIODS.items():
        da = int(pd.Timestamp(a).timestamp() // 86400)
        db = min(int(pd.Timestamp(b).timestamp() // 86400), last)
        sig = signals(syms, ranks, 0.20, da, db).sort_values("day").reset_index(drop=True)
        ls = fetch_ls([(r.sym, (r.day + 1) * 86400) for r in sig.itertuples()])
        sig["ls"] = [ls.get((r.sym, (r.day + 1) * 86400), np.nan) for r in sig.itertuples()]
        sig["majors"] = [majors_day_return(syms, d) for d in sig.day]
        data[pname] = (sig, g.Paths(syms, sig, g.MAX_H), da, db)
    for label, rule in rules:
        cells = []
        for pname, (sig, P, da, db) in data.items():
            m = ((sig.ls > 1.5).to_numpy() & rule(sig.majors.to_numpy())).copy()
            r = g.run_combo(sig, P, m, 0.15, 0.15, 3, True, da, db, margin_frac=0.15, return_curve=True)
            cells.append("—" if r is None else
                         f"{r['trades']}, {pct(r['mean'])}, ${r['curve'][-1] * 10_000:,.0f} ({pct(r['maxdd'], 0)})")
        out.append(f"| {label} | " + " | ".join(cells) + " |")
        print(out[-1], flush=True)
    # how the signals split by majors' day, per trade (whole 2024-26 period, same exits)
    sig, P, da, db = data["2024-01..2026-10"]
    ret, j, why, _ = P.outcome(0.15, 0.15, 3)
    t = sig.assign(ret=ret)[(sig.ls > 1.5) & ~np.isnan(ret)]
    t["bucket"] = pd.cut(t.majors, [-1, -0.03, -0.01, 0.01, 0.03, 0.05, 0.07, 1],
                         labels=["majors < −3%", "−3 to −1%", "−1 to +1%", "+1 to +3%", "+3 to +5%", "+5 to +7%",
                                 "majors > +7%"])
    out.append("\n### Per-signal return by the majors' day (2024-01..2026-10, before one-position-per-coin and drop rules)\n")
    out.append("| majors' day | signals | per trade | win |")
    out.append("|---|---|---|---|")
    for k, x in t.groupby("bucket", observed=True):
        out.append(f"| {k} | {len(x)} | {pct(x.ret.mean())} | {(x.ret > 0).mean():.0%} |")
    (HERE / "results" / "majors_filter.md").write_text("\n".join(out) + "\n")
    print("\n".join(out[-8:]))


if __name__ == "__main__":
    main()

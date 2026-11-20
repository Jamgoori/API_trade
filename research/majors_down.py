"""Pumps on days the majors fell: do they keep running? Per signal by the BTC/ETH/SOL average day move, and the
portfolio with different lower bounds for the majors filter (upper bound +7% kept).

Rules otherwise as in STRATEGY.md (incl. fallback take-profit at the 09:00 level), main + listing, 12%, $10k.
"""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from combined import PERIODS, portfolio, sleeve_trades
from fallback_tp import trades_with_tp
from funding_filter import funding_at_entry, skip_rule
from limit_entry import variant
from short_study import H_DAY, load, pct, volume_ranks
from stop_and_hold import load_signals
from wick_filter import day_shape

HERE = Path(__file__).resolve().parent


def tp_rows(m, syms, slip):
    fb = ~m.partial.to_numpy()
    ref = np.array([syms[r.sym].open[(r.day + 1) * H_DAY - syms[r.sym].t0] for r in m.itertuples()])
    fill = np.array([syms[r.sym].open[int(r.entry_h) - syms[r.sym].t0] for r in m.itertuples()]) * (1 - slip)
    a = np.round(1 - ref * 1.005 * 0.85 / fill, 2)
    tp = np.full(len(m), 0.15)
    below = fb & (fill < ref)
    tp[below] = np.where(a[below] < 0.01, np.nan, np.clip(a[below], 0.01, 0.15))   # skip if already at the level
    return tp


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    da = int(pd.Timestamp("2024-01-01").timestamp() // 86400)
    sig = load_signals(syms, ranks, da, last)
    sig["retrace"] = [day_shape(syms[r.sym], r.day)[1] for r in sig.itertuples()]
    sig["skipf"] = [skip_rule(*funding_at_entry(syms[r.sym], (r.day + 1) * H_DAY)) for r in sig.itertuples()]
    sig = sig[(sig.ls > 1.5) & (sig.retrace <= 0.30) & ~sig.skipf].reset_index(drop=True)   # majors left open
    ls = pd.read_csv(HERE / "results" / "listings.csv", parse_dates=["listed"])
    lh = (ls.listed - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(hours=1)
    lst = pd.DataFrame({"sym": ls.perp, "entry_h": lh.astype(int) + 1})
    lst = lst[[s in syms and syms[s].t0 <= h < syms[s].t0 + syms[s].n for s, h in zip(lst.sym, lst.entry_h)]]
    lst["day"] = lst.entry_h // H_DAY - 1
    split = pd.Timestamp("2025-07-01").timestamp() // 86400
    out = ["## Pumps on days the majors fell (main + listing, 12%, $10k)\n"]
    for slip, ss in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP, g.STOP_SLIP = slip, ss
        m, _ = variant(syms, sig, 0.005, 1, True, slip)
        tp = tp_rows(m, syms, slip)
        # per signal: one outcome per distinct take-profit, picked per row
        P = g.Paths(syms, m, g.MAX_H)
        ret = np.full(len(m), np.nan)
        for x in sorted(set(tp[~np.isnan(tp)])):
            r = P.outcome(float(x), 0.15, 2)[0]
            ret[tp == x] = r[tp == x]
        out += [f"### Per signal by majors day move, slippage {slip:.1%}\n",
                "| BTC/ETH/SOL average that day | 2024-01..2025-06 | 2025-07..2026-10 |", "|---|---|---|"]
        for lo, hi in ((-1, -0.05), (-0.05, -0.03), (-0.03, -0.02), (-0.02, -0.01), (-0.01, 0.0), (0.0, 0.03),
                       (0.03, 0.07), (0.07, 1)):
            mm = ((m.majors > lo) & (m.majors <= hi)).to_numpy()
            cells = []
            for early in (True, False):
                sel = mm & ((m.day < split) if early else (m.day >= split)).to_numpy() & ~np.isnan(ret)
                cells.append(f"{sel.sum()}, {pct(np.mean(ret[sel]))}, win {np.mean(ret[sel] > 0):.0%}" if sel.any() else "—")
            label = f"{'below' if lo == -1 else pct(lo, 0)} .. {'above' if hi == 1 else pct(hi, 0)}"
            out.append(f"| {label} | " + " | ".join(cells) + " |")
            print(out[-1], flush=True)
        out.append("")
        lt = [t + (True,) for t in sleeve_trades("listing", lst, syms, 0.15, 0.15, 2, False)]
        out += [f"### Portfolio by the majors filter's lower bound, slippage {slip:.1%}\n",
                "| skip signals when the majors fell more than | main trades | " + " | ".join(PERIODS) + " |",
                "|---|---|" + "---|" * len(PERIODS)]
        for lo in (-0.05, -0.03, -0.02, -0.01, 0.0):
            keep = ((m.majors >= lo) & (m.majors <= 0.07)).to_numpy()
            mt = trades_with_tp("main", m[keep].reset_index(drop=True), syms, tp[keep])
            cells = []
            for pname, (a0, b0) in PERIODS.items():
                pa = int(pd.Timestamp(a0).timestamp() // 86400)
                pb = min(int(pd.Timestamp(b0).timestamp() // 86400), last)
                fin, dd, _ = portfolio([t[:6] for t in mt + lt], pa, pb, 0.12, ("main", "listing"))
                cells.append(f"${fin:,.0f} ({pct(dd, 0)})")
            out.append(f"| {pct(lo, 0)}{' (now)' if lo == -0.03 else ''} | {len(mt)} | " + " | ".join(cells) + " |")
            print(out[-1], flush=True)
        out.append("")
    g.SLIP, g.STOP_SLIP = 0.001, 0.005
    (HERE / "results" / "majors_down.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

"""Universe cutoff: short coins outside the top N by trailing 30-day volume, for N = 30/50/70/100/150.

Rule as in STRATEGY.md (TP −15%, stop +15%, 48h, drop after stop, ratio > 1.5, majors filter), 10% margin.
"""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from short_study import H_DAY, load, pct, volume_ranks
from stop_and_hold import load_signals

HERE = Path(__file__).resolve().parent
PERIODS = {"2024-01..2025-06": ("2024-01-01", "2025-06-30"), "2025-07..2026-10": ("2025-07-01", "2026-10-31"),
           "2024-01..2026-10": ("2024-01-01", "2026-10-31")}


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    data = {}
    for pname, (a, b) in PERIODS.items():
        da = int(pd.Timestamp(a).timestamp() // 86400)
        db = min(int(pd.Timestamp(b).timestamp() // 86400), last)
        sig = load_signals(syms, ranks, da, db, min_rank=0)
        base = ((sig.ls > 1.5) & (sig.majors <= 0.07) & (sig.majors >= -0.03)).to_numpy()
        data[pname] = (sig, g.Paths(syms, sig, g.MAX_H), base, da, db)
    out = ["## Volume-rank cutoff (exits as in STRATEGY.md, 10% margin, $10k)\n",
           "### Per signal by volume rank band (filters applied, before the one-position and drop rules)\n",
           "| rank band | " + " | ".join(list(PERIODS)[:2]) + " |", "|---|---|---|"]
    for lo, hi in ((0, 30), (30, 50), (50, 70), (70, 100), (100, 150), (150, 10_000)):
        cells = []
        for pname in list(PERIODS)[:2]:
            sig, P, base, da, db = data[pname]
            ret = P.outcome(0.15, 0.15, 2)[0]
            sel = base & (sig["rank"] > lo).to_numpy() & (sig["rank"] <= hi).to_numpy() & ~np.isnan(ret)
            cells.append(f"{sel.sum()}, {pct(np.mean(ret[sel]))}, win {np.mean(ret[sel] > 0):.0%}" if sel.any() else "—")
        out.append(f"| {lo + 1}–{hi if hi < 10_000 else ''} | " + " | ".join(cells) + " |")
    out += ["", "### Portfolio by cutoff\n",
            "| short coins ranked below | " + " | ".join(PERIODS) + " | whole period, 0.3% slippage |", "|---|" + "---|" * (len(PERIODS) + 1)]
    for cut in (30, 50, 70, 100, 150):
        cells, slow = [], None
        for pname, (sig, P, base, da, db) in data.items():
            m = (base & (sig["rank"] > cut).to_numpy()).copy()
            r = g.run_combo(sig, P, m, 0.15, 0.15, 2, True, da, db, margin_frac=0.10, return_curve=True)
            cells.append(f"{r['trades']}, {pct(r['mean'])}, ${r['curve'][-1] * 10_000:,.0f} ({pct(r['maxdd'], 0)})")
            if pname == "2024-01..2026-10":
                g.SLIP, g.STOP_SLIP = 0.003, 0.01
                P3 = g.Paths(syms, sig, g.MAX_H)
                slow = g.run_combo(sig, P3, m, 0.15, 0.15, 2, True, da, db, margin_frac=0.10, return_curve=True)
                g.SLIP, g.STOP_SLIP = 0.001, 0.005
        out.append(f"| {cut} | " + " | ".join(cells) + f" | ${slow['curve'][-1] * 10_000:,.0f} ({pct(slow['maxdd'], 0)}) |")
        print(out[-1], flush=True)
    (HERE / "results" / "rank_cutoff.md").write_text("\n".join(out) + "\n")
    print("\n".join(out[:12]))


if __name__ == "__main__":
    main()

"""Should a +20% day that had spiked much higher (big upper wick) be skipped?

For each signal day: peak gain = day high / previous close − 1, retrace = 1 − close / day high.
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


def day_shape(s, day):
    a, b = day * H_DAY - s.t0, (day + 1) * H_DAY - s.t0
    prev, hi, close = s.close[a - 1], s.high[a:b].max(), s.close[b - 1]
    return hi / prev - 1, 1 - close / hi


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    data = {}
    for pname, (a, b) in PERIODS.items():
        da = int(pd.Timestamp(a).timestamp() // 86400)
        db = min(int(pd.Timestamp(b).timestamp() // 86400), last)
        sig = load_signals(syms, ranks, da, db)
        shp = [day_shape(syms[r.sym], r.day) for r in sig.itertuples()]
        sig["peak"] = [x[0] for x in shp]
        sig["retrace"] = [x[1] for x in shp]
        base = ((sig.ls > 1.5) & (sig.majors <= 0.07) & (sig.majors >= -0.03)).to_numpy()
        data[pname] = (sig, g.Paths(syms, sig, g.MAX_H), base, da, db)

    out = ["## Upper wick on the signal day (exits as in STRATEGY.md)\n",
           "### Per signal, by how far the close fell back from the day's high (filters applied, before the "
           "one-position and drop rules)\n",
           "| retrace from the day's high | " + " | ".join(list(PERIODS)[:2]) + " |", "|---|---|---|"]
    edges = [-0.01, 0.10, 0.20, 0.30, 1.0]
    labels = ["< 10% (closed near the high)", "10–20%", "20–30%", "> 30% (big wick)"]
    for lab, lo, hi in zip(labels, edges[:-1], edges[1:]):
        cells = []
        for pname in list(PERIODS)[:2]:
            sig, P, base, da, db = data[pname]
            ret = P.outcome(0.15, 0.15, 2)[0]
            sel = base & (sig.retrace > lo).to_numpy() & (sig.retrace <= hi).to_numpy() & ~np.isnan(ret)
            cells.append(f"{sel.sum()}, {pct(np.mean(ret[sel]))}, win {np.mean(ret[sel] > 0):.0%}" if sel.any() else "—")
        out.append(f"| {lab} | " + " | ".join(cells) + " |")
    out += ["", "### Per signal, by the day's peak gain\n", "| peak gain | " + " | ".join(list(PERIODS)[:2]) + " |", "|---|---|---|"]
    edges = [0.0, 0.30, 0.50, 0.80, 100]
    labels = ["+20–30%", "+30–50%", "+50–80%", "> +80%"]
    for lab, lo, hi in zip(labels, edges[:-1], edges[1:]):
        cells = []
        for pname in list(PERIODS)[:2]:
            sig, P, base, da, db = data[pname]
            ret = P.outcome(0.15, 0.15, 2)[0]
            sel = base & (sig.peak > lo).to_numpy() & (sig.peak <= hi).to_numpy() & ~np.isnan(ret)
            cells.append(f"{sel.sum()}, {pct(np.mean(ret[sel]))}, win {np.mean(ret[sel] > 0):.0%}" if sel.any() else "—")
        out.append(f"| {lab} | " + " | ".join(cells) + " |")

    out += ["", "### Portfolio with a skip rule (10% margin, $10k)\n",
            "| rule | " + " | ".join(PERIODS) + " | whole period, 0.3% slippage |", "|---|" + "---|" * (len(PERIODS) + 1)]
    rules = [("none (current)", lambda s: np.ones(len(s), bool)),
             ("skip if retrace > 30%", lambda s: (s.retrace <= 0.30).to_numpy()),
             ("skip if retrace > 20%", lambda s: (s.retrace <= 0.20).to_numpy()),
             ("skip if peak > +80% and retrace > 30%", lambda s: ~((s.peak > 0.80) & (s.retrace > 0.30)).to_numpy()),
             ("skip if peak > +50%", lambda s: (s.peak <= 0.50).to_numpy()),
             ("only retrace > 20% (reverse)", lambda s: (s.retrace > 0.20).to_numpy())]
    for lab, fn in rules:
        cells, slow = [], None
        for pname, (sig, P, base, da, db) in data.items():
            m = (base & fn(sig)).copy()
            r = g.run_combo(sig, P, m, 0.15, 0.15, 2, True, da, db, margin_frac=0.10, return_curve=True)
            cells.append(f"{r['trades']}, {pct(r['mean'])}, ${r['curve'][-1] * 10_000:,.0f} ({pct(r['maxdd'], 0)})")
            if pname == "2024-01..2026-10":
                g.SLIP, g.STOP_SLIP = 0.003, 0.01
                P3 = g.Paths(syms, sig, g.MAX_H)
                slow = g.run_combo(sig, P3, m, 0.15, 0.15, 2, True, da, db, margin_frac=0.10, return_curve=True)
                g.SLIP, g.STOP_SLIP = 0.001, 0.005
        out.append(f"| {lab} | " + " | ".join(cells) + f" | ${slow['curve'][-1] * 10_000:,.0f} ({pct(slow['maxdd'], 0)}) |")
    (HERE / "results" / "wick_filter.md").write_text("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    main()

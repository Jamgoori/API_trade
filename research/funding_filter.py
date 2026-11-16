"""Skip a signal when funding at entry is very negative (shorts paying heavily).

Funding at entry = the latest settled funding rate at or before the 00:00 UTC entry; its interval is inferred from
the gap to the previous settlement. Skip if rate <= −1% on a 4h (or 8h) interval, or <= −0.5% on a 1h interval.
Rule as in STRATEGY.md (incl. the wick filter), 10% margin, market entry at 00:00.
"""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from short_study import H_DAY, load, pct, volume_ranks
from stop_and_hold import load_signals
from wick_filter import day_shape

HERE = Path(__file__).resolve().parent
PERIODS = {"2024-01..2025-06": ("2024-01-01", "2025-06-30"), "2025-07..2026-10": ("2025-07-01", "2026-10-31"),
           "2024-01..2026-10": ("2024-01-01", "2026-10-31")}


def funding_at_entry(s, entry_abs_h: int) -> tuple[float, float]:
    i = entry_abs_h - s.t0
    ev = np.flatnonzero(s.fund[max(i - 48, 0):i + 1] != 0)
    if not len(ev):
        return np.nan, np.nan
    last = ev[-1] + max(i - 48, 0)
    interval = float(np.diff(ev)[-1]) if len(ev) > 1 else np.nan
    return float(s.fund[last]), interval


def skip_rule(rate, interval):
    if np.isnan(rate):
        return False
    if interval == 1:
        return rate <= -0.005
    return rate <= -0.01


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    out = ["## Funding filter (skip if funding at entry <= −1% on 4h/8h, <= −0.5% on 1h)\n",
           "Per cell: trades, per trade, $10k at 10% margin → (max DD).\n",
           "| rule | slippage | " + " | ".join(PERIODS) + " |", "|---|---|" + "---|" * len(PERIODS)]
    rows = {}
    for pname, (a, b) in PERIODS.items():
        da = int(pd.Timestamp(a).timestamp() // 86400)
        db = min(int(pd.Timestamp(b).timestamp() // 86400), last)
        sig = load_signals(syms, ranks, da, db)
        sig["retrace"] = [day_shape(syms[r.sym], r.day)[1] for r in sig.itertuples()]
        fi = [funding_at_entry(syms[r.sym], (r.day + 1) * H_DAY) for r in sig.itertuples()]
        sig["fund_now"], sig["fund_int"] = [x[0] for x in fi], [x[1] for x in fi]
        sig["skip_fund"] = [skip_rule(a_, b_) for a_, b_ in fi]
        base = ((sig.ls > 1.5) & (sig.majors <= 0.07) & (sig.majors >= -0.03) & (sig.retrace <= 0.30)).to_numpy()
        if pname == "2024-01..2026-10":
            P = g.Paths(syms, sig, g.MAX_H)
            ret = P.outcome(0.15, 0.15, 2)[0]
            sk = base & sig.skip_fund.to_numpy() & ~np.isnan(ret)
            out.insert(1, f"Signals the rule removes (2024-01..2026-10): {sk.sum()} of {(base & ~np.isnan(ret)).sum()}, "
                          f"their average result {pct(np.mean(ret[sk])) if sk.any() else '—'}.\n")
        for slip, stop_slip in ((0.001, 0.005), (0.003, 0.01)):
            g.SLIP, g.STOP_SLIP = slip, stop_slip
            P = g.Paths(syms, sig, g.MAX_H)
            for lab, m in (("current", base), ("+ funding filter", base & ~sig.skip_fund.to_numpy())):
                r = g.run_combo(sig, P, m.copy(), 0.15, 0.15, 2, True, da, db, margin_frac=0.10, return_curve=True)
                rows.setdefault((lab, slip), []).append(
                    f"{r['trades']}, {pct(r['mean'])}, ${r['curve'][-1] * 10_000:,.0f} ({pct(r['maxdd'], 0)})")
        g.SLIP, g.STOP_SLIP = 0.001, 0.005
    for (lab, slip), cells in rows.items():
        out.append(f"| {lab} | {slip:.1%} | " + " | ".join(cells) + " |")
    (HERE / "results" / "funding_filter.md").write_text("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    main()

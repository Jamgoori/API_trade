"""The other side: are there +20% daily pumps worth buying (long) instead of shorting?

Long at the 00:00 UTC (09:00 KST) open after the pump day, market order. Exits: TP +15% / stop −15% / 48h, or
TP +30% / stop −15% / 7 days. Fees (taker in, maker on TP, taker otherwise), slippage and funding (a long pays
positive funding) included; a bar touching both levels counts as the stop.
"""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from funding_filter import funding_at_entry
from pump_day_short import daily_close_index
from short_study import FEE, H_DAY, load, pct, volume_ranks
from stop_and_hold import load_signals
from tp_grid import MAKER

HERE = Path(__file__).resolve().parent


def long_outcome(P, tp, stop, hold_h, slip, stop_slip):
    n = len(P.valid)
    rows = np.arange(n)
    end = np.minimum(hold_h, P.valid)
    ok = (P.valid >= hold_h) | (P.delisted & (P.valid >= 2))
    with np.errstate(invalid="ignore"):
        ht, hs = P.H >= 1 + tp, P.L <= 1 - stop
    jt = np.where(ht.any(1), np.argmax(ht, 1), P.max_h)
    js = np.where(hs.any(1), np.argmax(hs, 1), P.max_h)
    js_in, jt_in = js < end, (jt < end) & (jt < js)
    j = np.clip(np.where(js_in & ~jt_in, js, np.where(jt_in, jt, end - 1)), 0, P.max_h - 1)
    o, fc = P.O[rows, j], P.FC[rows, j]
    x = np.where(js_in & ~jt_in, np.minimum(1 - stop, o) * (1 - stop_slip),
                 np.where(jt_in, 1 + tp, P.C[rows, j] * (1 - slip)))
    fee_out = np.where(jt_in, MAKER, FEE) * x
    ret = (x - 1) - FEE - fee_out - fc
    return np.where(ok, ret, np.nan)


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    da = int(pd.Timestamp("2024-01-01").timestamp() // 86400)
    sig = load_signals(syms, ranks, da, last, min_rank=0)
    prev = []
    for r in sig.itertuples():
        s = syms[r.sym]
        k1, k2 = daily_close_index(s, r.day - 1), daily_close_index(s, r.day - 2)
        prev.append(s.close[k1] / s.close[k2] - 1 if k1 is not None and k2 is not None else np.nan)
    sig["prev_ret"] = prev
    sig["funding"] = [funding_at_entry(syms[r.sym], (r.day + 1) * H_DAY)[0] for r in sig.itertuples()]
    split = pd.Timestamp("2025-07-01").timestamp() // 86400
    groups = {
        "all +20% pumps, rank 101+": sig["rank"] > 100,
        "all +20% pumps, rank 1-100": sig["rank"] <= 100,
        "L/S ratio <= 1.0": sig.ls <= 1.0,
        "L/S ratio <= 0.8": sig.ls <= 0.8,
        "L/S ratio 1.0-1.5": (sig.ls > 1.0) & (sig.ls <= 1.5),
        "L/S ratio > 1.5 (our shorts)": sig.ls > 1.5,
        "majors fell 3%+": sig.majors < -0.03,
        "majors fell 2%+": sig.majors < -0.02,
        "majors up 7%+": sig.majors > 0.07,
        "day before +10-20%": (sig.prev_ret >= 0.1) & (sig.prev_ret < 0.2),
        "day +50% or more": sig.day_ret >= 0.5,
        "L/S <= 1.0 and majors fell 2%+": (sig.ls <= 1.0) & (sig.majors < -0.02),
        "L/S <= 1.0 and rank 101+": (sig.ls <= 1.0) & (sig["rank"] > 100),
        "funding negative (shorts paying)": sig.funding < 0,
    }
    out = ["## Long the +20% pumps? (entry 09:00 KST open, market)\n",
           "Per cell: signals, average return on margin, win rate.\n"]
    for slip, ss in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP = 0.0  # Paths divides by (1 - SLIP); entry_px below already holds the long's fill
        sig["entry_px"] = [syms[r.sym].open[(r.day + 1) * H_DAY - syms[r.sym].t0] * (1 + slip) for r in sig.itertuples()]
        P = g.Paths(syms, sig, 7 * H_DAY + 1)
        res = {"TP +15% / stop −15% / 48h": long_outcome(P, 0.15, 0.15, 48, slip, ss),
               "TP +30% / stop −15% / 7d": long_outcome(P, 0.30, 0.15, 7 * 24, slip, ss)}
        for xname, ret in res.items():
            out += [f"### {xname}, slippage {slip:.1%}\n", "| group | 2024-01..2025-06 | 2025-07..2026-10 |", "|---|---|---|"]
            for name, m in groups.items():
                cells = []
                for early in (True, False):
                    sel = m.to_numpy() & ((sig.day < split) if early else (sig.day >= split)).to_numpy() & ~np.isnan(ret)
                    cells.append(f"{sel.sum()}, {pct(np.mean(ret[sel]))}, win {np.mean(ret[sel] > 0):.0%}" if sel.sum() >= 5 else f"{sel.sum()}, —")
                out.append(f"| {name} | " + " | ".join(cells) + " |")
            out.append("")
            print("\n".join(out[-len(groups) - 4:]), flush=True)
    (HERE / "results" / "long_side.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

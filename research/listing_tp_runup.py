"""Listing shorts: does a coin that rose little before the listing need a closer take-profit than −15%?

Take-profit grid by the 24h run-up before entry (entry = listing hour +1h), stop +15%, 48h. Then the whole strategy
with a closer take-profit only for low run-ups.
"""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from combined import sleeve_trades
from fallback_tp import trades_with_tp
from funding_filter import funding_at_entry, skip_rule
from limit_entry import variant
from ls_tiered import WINDOWS, portfolio_w
from majors_down import tp_rows
from short_study import H_DAY, load, pct, volume_ranks
from stop_and_hold import load_signals
from wick_filter import day_shape

HERE = Path(__file__).resolve().parent
SPLIT = pd.Timestamp("2025-07-01").timestamp() // 3600
BANDS = ((-9, 0.10, "below +10%"), (0.10, 99, "+10% or more"), (-9, 0.0, "  below 0"), (0.0, 0.05, "  0..+5%"),
         (0.05, 0.10, "  +5..+10%"))
TPS = (0.05, 0.08, 0.10, 0.12, 0.15, 0.20)


def listing_frame(syms):
    ls = pd.read_csv(HERE / "results" / "listings.csv", parse_dates=["listed"])
    lh = (ls.listed - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(hours=1)
    lst = pd.DataFrame({"sym": ls.perp, "entry_h": lh.astype(int) + 1, "venue": ls.venue})
    lst = lst[[s in syms and syms[s].t0 + 24 <= h < syms[s].t0 + syms[s].n for s, h in zip(lst.sym, lst.entry_h)]]
    lst = lst.reset_index(drop=True)
    lst["day"] = lst.entry_h // H_DAY - 1
    lst["runup"] = [syms[r.sym].open[r.entry_h - syms[r.sym].t0] / syms[r.sym].open[r.entry_h - 24 - syms[r.sym].t0] - 1
                    for r in lst.itertuples()]
    return lst


def main() -> None:
    syms = load()
    lst = listing_frame(syms)
    early = (lst.entry_h < SPLIT).to_numpy()
    out = ["## Listing shorts: take-profit by the 24h run-up before entry (stop +15%, 48h)\n"]
    for slip, ss in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP, g.STOP_SLIP = slip, ss
        P = g.Paths(syms, lst, g.MAX_H)
        res = {tp: P.outcome(tp, 0.15, 2) for tp in TPS}
        out += [f"### Slippage {slip:.1%}: per trade (early / late half), share reaching the take-profit\n",
                "| run-up | trades | " + " | ".join(f"TP {tp:.0%}" for tp in TPS) + " |", "|---|---|" + "---|" * len(TPS)]
        for lo, hi, label in BANDS:
            b = ((lst.runup >= lo) & (lst.runup < hi)).to_numpy() & ~np.isnan(res[0.15][0])
            cells = []
            for tp in TPS:
                r, why = res[tp][0], res[tp][2]
                cells.append(f"{pct(r[b].mean())} ({pct(r[b & early].mean(), 0)} / {pct(r[b & ~early].mean(), 0)}), "
                             f"TP {np.mean(why[b] == 1):.0%}")
            out.append(f"| {label} | {b.sum()} | " + " | ".join(cells) + " |")
        out.append("")
        print("\n".join(out[-len(BANDS) - 4:]), flush=True)

    # whole strategy: closer take-profit only for listings that rose less than 10%
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    da = int(pd.Timestamp("2024-01-01").timestamp() // 86400)
    sig = load_signals(syms, ranks, da, last)
    sig["retrace"] = [day_shape(syms[r.sym], r.day)[1] for r in sig.itertuples()]
    sig["skipf"] = [skip_rule(*funding_at_entry(syms[r.sym], (r.day + 1) * H_DAY)) for r in sig.itertuples()]
    sig = sig[(sig.ls > 1.5) & (sig.ls <= 3.0) & (sig.majors <= 0.07) & (sig.majors >= -0.03) & (sig.retrace <= 0.30)
              & ~sig.skipf].reset_index(drop=True)
    low = lst[lst.runup < 0.10]
    high = lst[lst.runup >= 0.10]
    for slip, ss in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP, g.STOP_SLIP = slip, ss
        m, _ = variant(syms, sig, 0.005, 1, True, slip)
        mt = trades_with_tp("main", m, syms, tp_rows(m, syms, slip))
        size = {(r.sym, int(e)): (0.12 if r.ls > 2.0 else 0.06) for r, e in zip(m.itertuples(), m.entry_h)}
        main_tr = [(t[1], t[2], t[3], t[4], t[5], size[(t[1], t[2])]) for t in mt]
        hi_tr = [(t[1], t[2], t[3], t[4], t[5], 0.12) for t in sleeve_trades("listing", high, syms, 0.15, 0.15, 2, False)]
        out += [f"### Whole strategy, slippage {slip:.1%} ($10k): take-profit for listings that rose less than 10%\n",
                "| take-profit (run-up < 10%) | " + " | ".join(WINDOWS) + " |", "|---|" + "---|" * len(WINDOWS)]
        for tp in (0.05, 0.08, 0.10, 0.12, 0.15):
            lo_tr = [(t[1], t[2], t[3], t[4], t[5], 0.12) for t in sleeve_trades("listing", low, syms, tp, 0.15, 2, False)]
            cells = []
            for a0, b0 in WINDOWS.values():
                pa = int(pd.Timestamp(a0).timestamp() // 86400)
                pb = min(int(pd.Timestamp(b0).timestamp() // 86400), last)
                fin, dd = portfolio_w(main_tr + hi_tr + lo_tr, pa, pb)
                cells.append(f"${fin:,.0f} ({pct(dd, 0)})")
            out.append(f"| {tp:.0%}{' (now)' if tp == 0.15 else ''} | " + " | ".join(cells) + " |")
            print(out[-1], flush=True)
        out.append("")
    g.SLIP, g.STOP_SLIP = 0.001, 0.005
    (HERE / "results" / "listing_tp_runup.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

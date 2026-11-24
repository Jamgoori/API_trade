"""Listing shorts by how much the perp rose in the 24 hours before the entry (entry = listing hour +1h).

Current exits (TP −15% / stop +15% / 48h). Then the whole strategy (main with 6%/12% by L/S and the 3.0 cap,
fallback take-profit; listing 12%) with listings skipped below a run-up threshold.
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
BANDS = ((-9, 0.0), (0.0, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.40), (0.40, 99))


def main() -> None:
    syms = load()
    ls = pd.read_csv(HERE / "results" / "listings.csv", parse_dates=["listed"])
    lh = (ls.listed - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(hours=1)
    lst = pd.DataFrame({"sym": ls.perp, "entry_h": lh.astype(int) + 1, "venue": ls.venue})
    lst = lst[[s in syms and syms[s].t0 + 24 <= h < syms[s].t0 + syms[s].n for s, h in zip(lst.sym, lst.entry_h)]]
    lst = lst.reset_index(drop=True)
    lst["day"] = lst.entry_h // H_DAY - 1
    lst["runup"] = [syms[r.sym].open[r.entry_h - syms[r.sym].t0] / syms[r.sym].open[r.entry_h - 24 - syms[r.sym].t0] - 1
                    for r in lst.itertuples()]
    out = ["## Listing shorts by the 24h run-up before entry\n"]
    for slip, ss in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP, g.STOP_SLIP = slip, ss
        ret = g.Paths(syms, lst, g.MAX_H).outcome(0.15, 0.15, 2)[0]
        early = (lst.entry_h < SPLIT).to_numpy()
        out += [f"### Per trade, slippage {slip:.1%}\n", "| 24h run-up | trades | per trade | win | early / late |",
                "|---|---|---|---|---|"]
        for lo, hi in BANDS:
            b = ((lst.runup >= lo) & (lst.runup < hi)).to_numpy() & ~np.isnan(ret)
            out.append(f"| {'below 0' if lo == -9 else pct(lo, 0)}..{'' if hi == 99 else pct(hi, 0)} | {b.sum()} | "
                       f"{pct(ret[b].mean())} | {np.mean(ret[b] > 0):.0%} | {pct(ret[b & early].mean())} / {pct(ret[b & ~early].mean())} |")
        out.append("")
        print("\n".join(out[-len(BANDS) - 4:]), flush=True)

    # whole strategy with listing run-up thresholds
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    da = int(pd.Timestamp("2024-01-01").timestamp() // 86400)
    sig = load_signals(syms, ranks, da, last)
    sig["retrace"] = [day_shape(syms[r.sym], r.day)[1] for r in sig.itertuples()]
    sig["skipf"] = [skip_rule(*funding_at_entry(syms[r.sym], (r.day + 1) * H_DAY)) for r in sig.itertuples()]
    sig = sig[(sig.ls > 1.5) & (sig.ls <= 3.0) & (sig.majors <= 0.07) & (sig.majors >= -0.03) & (sig.retrace <= 0.30)
              & ~sig.skipf].reset_index(drop=True)
    for slip, ss in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP, g.STOP_SLIP = slip, ss
        m, _ = variant(syms, sig, 0.005, 1, True, slip)
        mt = trades_with_tp("main", m, syms, tp_rows(m, syms, slip))
        size = {(r.sym, int(e)): (0.12 if r.ls > 2.0 else 0.06) for r, e in zip(m.itertuples(), m.entry_h)}
        main_tr = [(t[1], t[2], t[3], t[4], t[5], size[(t[1], t[2])]) for t in mt]
        out += [f"### Whole strategy, slippage {slip:.1%} ($10k)\n", "| listings taken | listing trades | " + " | ".join(WINDOWS) + " |",
                "|---|---|" + "---|" * len(WINDOWS)]
        for label, th in (("all (now)", -9), ("run-up 0% or more", 0.0), ("run-up 5% or more", 0.05),
                          ("run-up 10% or more", 0.10), ("no listings", 99)):
            keep = lst[lst.runup >= th] if th < 99 else lst.iloc[0:0]
            lt = [(t[1], t[2], t[3], t[4], t[5], 0.12) for t in sleeve_trades("listing", keep, syms, 0.15, 0.15, 2, False)] if len(keep) else []
            cells = []
            for a0, b0 in WINDOWS.values():
                pa = int(pd.Timestamp(a0).timestamp() // 86400)
                pb = min(int(pd.Timestamp(b0).timestamp() // 86400), last)
                fin, dd = portfolio_w(main_tr + lt, pa, pb)
                cells.append(f"${fin:,.0f} ({pct(dd, 0)})")
            out.append(f"| {label} | {len(lt)} | " + " | ".join(cells) + " |")
            print(out[-1], flush=True)
        out.append("")
    g.SLIP, g.STOP_SLIP = 0.001, 0.005
    (HERE / "results" / "listing_runup.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

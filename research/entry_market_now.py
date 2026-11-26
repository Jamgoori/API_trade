"""Main rule entry: 09:00 KST market short vs the current +0.5% limit with the 10:00 IOC fallback, in the whole
strategy as it stands (L/S 6%/12% sizing with the 3.0 cap, fallback take-profit, listing sleeve at 12%)."""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from combined import sleeve_trades
from fallback_tp import trades_with_tp
from funding_filter import funding_at_entry, skip_rule
from limit_entry import variant
from listing_tp_runup import listing_frame
from ls_tiered import WINDOWS, portfolio_w
from majors_down import tp_rows
from short_study import H_DAY, load, pct, volume_ranks
from stop_and_hold import load_signals
from wick_filter import day_shape

HERE = Path(__file__).resolve().parent


def main() -> None:
    syms = load()
    lst = listing_frame(syms)
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    da = int(pd.Timestamp("2024-01-01").timestamp() // 86400)
    sig = load_signals(syms, ranks, da, last)
    sig["retrace"] = [day_shape(syms[r.sym], r.day)[1] for r in sig.itertuples()]
    sig["skipf"] = [skip_rule(*funding_at_entry(syms[r.sym], (r.day + 1) * H_DAY)) for r in sig.itertuples()]
    sig = sig[(sig.ls > 1.5) & (sig.ls <= 3.0) & (sig.majors <= 0.07) & (sig.majors >= -0.03) & (sig.retrace <= 0.30)
              & ~sig.skipf].reset_index(drop=True)
    out = ["## Main entry: 09:00 market vs +0.5% limit then 10:00 IOC (whole strategy, $10k)\n",
           "| market slippage | entry | main trades | per trade | " + " | ".join(WINDOWS) + " |",
           "|---|---|---|---|" + "---|" * len(WINDOWS)]
    for slip in (0.001, 0.003, 0.005, 0.008):
        g.SLIP, g.STOP_SLIP = slip, max(0.005, slip * 2)
        lt = [(t[1], t[2], t[3], t[4], t[5], 0.12) for t in sleeve_trades("listing", lst, syms, 0.15, 0.15, 2, False)]
        for label, offset in (("09:00 market", None), ("+0.5% limit, 10:00 IOC (now)", 0.005)):
            m, _ = variant(syms, sig, offset, 1, True, slip)
            if "partial" not in m:
                m["partial"] = False
            tp = tp_rows(m, syms, slip) if offset is not None else np.full(len(m), 0.15)
            mt = trades_with_tp("main", m, syms, tp)
            size = {(r.sym, int(e)): (0.12 if r.ls > 2.0 else 0.06) for r, e in zip(m.itertuples(), m.entry_h)}
            main_tr = [(t[1], t[2], t[3], t[4], t[5], size[(t[1], t[2])]) for t in mt]
            cells = []
            for a0, b0 in WINDOWS.values():
                pa = int(pd.Timestamp(a0).timestamp() // 86400)
                pb = min(int(pd.Timestamp(b0).timestamp() // 86400), last)
                fin, dd = portfolio_w(main_tr + lt, pa, pb)
                cells.append(f"${fin:,.0f} ({pct(dd, 0)})")
            out.append(f"| {slip:.1%} | {label} | {len(mt)} | {pct(np.mean([t[4] for t in mt]))} | " + " | ".join(cells) + " |")
            print(out[-1], flush=True)
    g.SLIP, g.STOP_SLIP = 0.001, 0.005
    (HERE / "results" / "entry_market_now.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

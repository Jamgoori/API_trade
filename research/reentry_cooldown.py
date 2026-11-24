"""Skip a short on a coin already shorted in the last N days (by either sleeve), e.g. a second KRW listing of the
same coin a few hours after the first listing short closed. Whole strategy as now (main 6%/12% by L/S with the 3.0
cap and fallback take-profit, listing 12%)."""

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


def cooldown(trades, hours):
    """Drop a trade whose coin had a trade entered in the `hours` before it (kept trades only)."""
    last, out, dropped = {}, [], []
    for t in sorted(trades, key=lambda t: t[1]):
        if t[0] in last and t[1] - last[t[0]] < hours:
            dropped.append(t)
            continue
        last[t[0]] = t[1]
        out.append(t)
    return out, dropped


def main() -> None:
    syms = load()
    ls = pd.read_csv(HERE / "results" / "listings.csv", parse_dates=["listed"])
    lh = (ls.listed - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(hours=1)
    lst = pd.DataFrame({"sym": ls.perp, "entry_h": lh.astype(int) + 1, "venue": ls.venue})
    lst = lst[[s in syms and syms[s].t0 + 24 <= h < syms[s].t0 + syms[s].n for s, h in zip(lst.sym, lst.entry_h)]]
    lst = lst.reset_index(drop=True)
    lst["day"] = lst.entry_h // H_DAY - 1
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    da = int(pd.Timestamp("2024-01-01").timestamp() // 86400)
    sig = load_signals(syms, ranks, da, last)
    sig["retrace"] = [day_shape(syms[r.sym], r.day)[1] for r in sig.itertuples()]
    sig["skipf"] = [skip_rule(*funding_at_entry(syms[r.sym], (r.day + 1) * H_DAY)) for r in sig.itertuples()]
    sig = sig[(sig.ls > 1.5) & (sig.ls <= 3.0) & (sig.majors <= 0.07) & (sig.majors >= -0.03) & (sig.retrace <= 0.30)
              & ~sig.skipf].reset_index(drop=True)
    out = ["## Skip a coin shorted in the last N days (either sleeve)\n"]
    for slip, ss in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP, g.STOP_SLIP = slip, ss
        m, _ = variant(syms, sig, 0.005, 1, True, slip)
        mt = trades_with_tp("main", m, syms, tp_rows(m, syms, slip))
        size = {(r.sym, int(e)): (0.12 if r.ls > 2.0 else 0.06) for r, e in zip(m.itertuples(), m.entry_h)}
        main_tr = [(t[1], t[2], t[3], t[4], t[5], size[(t[1], t[2])]) for t in mt]
        lt = [(t[1], t[2], t[3], t[4], t[5], 0.12) for t in sleeve_trades("listing", lst, syms, 0.15, 0.15, 2, False)]
        out += [f"### Slippage {slip:.1%} ($10k)\n", "| rule | trades dropped | their average | " + " | ".join(WINDOWS) + " |",
                "|---|---|---|" + "---|" * len(WINDOWS)]
        for label, hours in (("now (re-enter once the earlier short is closed)", 0), ("skip if shorted in last 3 days", 72),
                             ("skip if shorted in last 7 days", 168)):
            trades, dropped = cooldown(main_tr + lt, hours)
            cells = []
            for a0, b0 in WINDOWS.values():
                pa = int(pd.Timestamp(a0).timestamp() // 86400)
                pb = min(int(pd.Timestamp(b0).timestamp() // 86400), last)
                fin, dd = portfolio_w(trades, pa, pb)
                cells.append(f"${fin:,.0f} ({pct(dd, 0)})")
            avg = pct(np.mean([t[3] for t in dropped])) if dropped else "—"
            out.append(f"| {label} | {len(dropped)} | {avg} | " + " | ".join(cells) + " |")
            print(out[-1], flush=True)
        out.append("")
    g.SLIP, g.STOP_SLIP = 0.001, 0.005
    (HERE / "results" / "reentry_cooldown.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

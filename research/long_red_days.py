"""Long the +20% pumps on days the majors fell: robustness and a combined portfolio with the short strategy."""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from combined import PERIODS, portfolio, sleeve_trades
from fallback_tp import trades_with_tp
from funding_filter import funding_at_entry, skip_rule
from limit_entry import variant
from long_side import long_outcome
from majors_down import tp_rows
from short_study import FEE, H_DAY, load, pct, volume_ranks
from stop_and_hold import load_signals
from wick_filter import day_shape

HERE = Path(__file__).resolve().parent


def long_trades(sig, syms, P, tp, stop, hold_h, slip, stop_slip):
    """(sleeve, sym, entry_h, exit_h, ret, daily mtm) for one long per coin at a time."""
    ret = long_outcome(P, tp, stop, hold_h, slip, stop_slip)
    n = len(sig)
    rows = np.arange(n)
    with np.errstate(invalid="ignore"):
        ht, hs = P.H >= 1 + tp, P.L <= 1 - stop
    jt = np.where(ht.any(1), np.argmax(ht, 1), P.max_h)
    js = np.where(hs.any(1), np.argmax(hs, 1), P.max_h)
    end = np.minimum(hold_h, P.valid)
    j = np.where((js < end) & ~((jt < end) & (jt < js)), js, np.where((jt < end) & (jt < js), jt, end - 1))
    out, free = [], {}
    for r in np.argsort(P.entry_h, kind="stable"):
        if np.isnan(ret[r]) or P.entry_h[r] <= free.get(sig.sym.iat[r], -1):
            continue
        free[sig.sym.iat[r]] = P.entry_h[r] + j[r]
        days = int(j[r] // H_DAY) + 2
        idx = np.clip(P.off0[r] + H_DAY * np.arange(days), 0, P.max_h - 1)
        mtm = (P.C[r, idx] - 1) - FEE - P.FC[r, idx]
        out.append(("long", sig.sym.iat[r], int(P.entry_h[r]), int(P.entry_h[r] + j[r]), float(ret[r]), mtm.astype(float)))
    return out


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    da = int(pd.Timestamp("2024-01-01").timestamp() // 86400)
    allsig = load_signals(syms, ranks, da, last, min_rank=0)
    split = pd.Timestamp("2025-07-01").timestamp() // 86400
    out = ["## Long +20% pumps on red days for the majors\n"]
    # robustness of the long idea
    for slip, ss in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP = 0.0
        lsig = allsig[allsig.majors < -0.02].reset_index(drop=True)
        lsig["entry_px"] = [syms[r.sym].open[(r.day + 1) * H_DAY - syms[r.sym].t0] * (1 + slip) for r in lsig.itertuples()]
        P = g.Paths(syms, lsig, 7 * H_DAY + 1)
        out += [f"### Robustness, slippage {slip:.1%} (per signal)\n",
                "| subset | days | 15/15/48h early | 15/15/48h late | 30/15/7d early | 30/15/7d late |", "|---|---|---|---|---|---|"]
        r1 = long_outcome(P, 0.15, 0.15, 48, slip, ss)
        r2 = long_outcome(P, 0.30, 0.15, 168, slip, ss)
        subsets = {"majors below −3%": lsig.majors < -0.03, "majors below −4%": lsig.majors < -0.04,
                   "majors below −5%": lsig.majors < -0.05, "majors −3%, rank 101+": (lsig.majors < -0.03) & (lsig["rank"] > 100),
                   "majors −3%, rank 1-100": (lsig.majors < -0.03) & (lsig["rank"] <= 100),
                   "majors −3%, day +20-50%": (lsig.majors < -0.03) & (lsig.day_ret < 0.5),
                   "majors −3%, day +50%+": (lsig.majors < -0.03) & (lsig.day_ret >= 0.5)}
        for name, m in subsets.items():
            m = m.to_numpy()
            cells = []
            for ret in (r1, r2):
                for early in (True, False):
                    sel = m & ((lsig.day < split) if early else (lsig.day >= split)).to_numpy() & ~np.isnan(ret)
                    cells.append(f"{sel.sum()}, {pct(np.mean(ret[sel]))}" if sel.sum() >= 5 else f"{sel.sum()}, —")
            out.append(f"| {name} | {lsig.day[m].nunique()} | " + " | ".join(cells) + " |")
        out.append("")
        print("\n".join(out[-len(subsets) - 4:]), flush=True)

    # combined portfolio: short main + listing as in STRATEGY.md, plus the long sleeve
    sig = allsig[allsig["rank"] > 100].copy()
    sig["retrace"] = [day_shape(syms[r.sym], r.day)[1] for r in sig.itertuples()]
    sig["skipf"] = [skip_rule(*funding_at_entry(syms[r.sym], (r.day + 1) * H_DAY)) for r in sig.itertuples()]
    sig = sig[(sig.ls > 1.5) & (sig.majors <= 0.07) & (sig.majors >= -0.03) & (sig.retrace <= 0.30) & ~sig.skipf].reset_index(drop=True)
    ls = pd.read_csv(HERE / "results" / "listings.csv", parse_dates=["listed"])
    lh = (ls.listed - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(hours=1)
    lst = pd.DataFrame({"sym": ls.perp, "entry_h": lh.astype(int) + 1})
    lst = lst[[s in syms and syms[s].t0 <= h < syms[s].t0 + syms[s].n for s, h in zip(lst.sym, lst.entry_h)]]
    lst["day"] = lst.entry_h // H_DAY - 1
    for slip, ss in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP, g.STOP_SLIP = slip, ss
        m, _ = variant(syms, sig, 0.005, 1, True, slip)
        mt = [t[:6] for t in trades_with_tp("main", m, syms, tp_rows(m, syms, slip))]
        lt = sleeve_trades("listing", lst, syms, 0.15, 0.15, 2, False)
        g.SLIP = 0.0
        lsig = allsig[allsig.majors < -0.03].reset_index(drop=True)
        lsig["entry_px"] = [syms[r.sym].open[(r.day + 1) * H_DAY - syms[r.sym].t0] * (1 + slip) for r in lsig.itertuples()]
        P = g.Paths(syms, lsig, 7 * H_DAY + 1)
        longs = {"15/15/48h": long_trades(lsig, syms, P, 0.15, 0.15, 48, slip, ss),
                 "30/15/7d": long_trades(lsig, syms, P, 0.30, 0.15, 168, slip, ss)}
        g.SLIP = slip
        out += [f"### Portfolio, slippage {slip:.1%} (12% per trade, $10k)\n",
                "| sleeves | " + " | ".join(PERIODS) + " |", "|---|" + "---|" * len(PERIODS)]
        combos = [("short main + listing (now)", mt + lt, ("main", "listing"))]
        for k, v in longs.items():
            combos.append((f"+ long red-day pumps {k}", mt + lt + v, ("main", "listing", "long")))
            combos.append((f"long red-day pumps {k} only", v, ("long",)))
        for label, tr, sl in combos:
            cells = []
            for pname, (a0, b0) in PERIODS.items():
                pa = int(pd.Timestamp(a0).timestamp() // 86400)
                pb = min(int(pd.Timestamp(b0).timestamp() // 86400), last)
                fin, dd, tk = portfolio(tr, pa, pb, 0.12, sl)
                cells.append(f"${fin:,.0f} ({pct(dd, 0)})")
            out.append(f"| {label} | " + " | ".join(cells) + " |")
            print(out[-1], flush=True)
        out.append("")
    (HERE / "results" / "long_red_days.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

"""Take-profit for entries made by the 01:00 UTC (10:00 KST) fallback: same −15% from the fill, a tighter one,
or the price level the +0.5% limit would have targeted (09:00 open × 1.005 × 0.85).

Everything else as in STRATEGY.md (main + listing, 12%, $10k). Stops stay +15% from the fill.
"""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from combined import PERIODS, portfolio
from funding_filter import funding_at_entry, skip_rule
from limit_entry import variant
from short_study import H_DAY, load, pct, volume_ranks
from stop_and_hold import load_signals
from wick_filter import day_shape
from combined import sleeve_trades

HERE = Path(__file__).resolve().parent
BINS = np.round(np.arange(0.04, 0.151, 0.01), 2)


def trades_with_tp(name, sig, syms, tp_row, ban=True):
    """Like combined.sleeve_trades, but each row has its own take-profit (rows with NaN are skipped)."""
    sig = sig.reset_index(drop=True)
    P = g.Paths(syms, sig, g.MAX_H)
    n = len(sig)
    ret, j, why = np.full(n, np.nan), np.zeros(n, int), np.zeros(n, int)
    mtm = [None] * n
    for tp in sorted(set(np.round(tp_row[~np.isnan(tp_row)], 2))):
        r, jj, w, m = P.outcome(float(tp), 0.15, 2)
        sel = np.round(tp_row, 2) == tp
        ret[sel], j[sel], why[sel] = r[sel], jj[sel], w[sel]
        for k in np.flatnonzero(sel):
            mtm[k] = m[k]
    order = np.argsort(P.entry_h, kind="stable")
    out, state = [], {}
    for r in order:
        if np.isnan(ret[r]):
            continue
        sym = sig.sym.iat[r]
        free, banned = state.get(sym, (-1, False))
        if banned or P.entry_h[r] <= free:
            continue
        state[sym] = (P.entry_h[r] + j[r], ban and why[r] >= 2)
        out.append((name, sym, int(P.entry_h[r]), int(P.entry_h[r] + j[r]), float(ret[r]), mtm[r].copy(),
                    bool(sig.partial.iat[r])))
    return out


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    da = int(pd.Timestamp("2024-01-01").timestamp() // 86400)
    sig = load_signals(syms, ranks, da, last)
    sig["retrace"] = [day_shape(syms[r.sym], r.day)[1] for r in sig.itertuples()]
    sig["skipf"] = [skip_rule(*funding_at_entry(syms[r.sym], (r.day + 1) * H_DAY)) for r in sig.itertuples()]
    sig = sig[(sig.ls > 1.5) & (sig.majors <= 0.07) & (sig.majors >= -0.03) & (sig.retrace <= 0.30) & ~sig.skipf]
    sig = sig.reset_index(drop=True)
    ls = pd.read_csv(HERE / "results" / "listings.csv", parse_dates=["listed"])
    lh = (ls.listed - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(hours=1)
    lst = pd.DataFrame({"sym": ls.perp, "entry_h": lh.astype(int) + 1})
    lst = lst[[s in syms and syms[s].t0 <= h < syms[s].t0 + syms[s].n for s, h in zip(lst.sym, lst.entry_h)]]
    lst["day"] = lst.entry_h // H_DAY - 1
    out = ["## Take-profit for 10:00 KST fallback entries (main + listing, 12%, $10k)\n"]
    for slip, ss in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP, g.STOP_SLIP = slip, ss
        m, _ = variant(syms, sig, 0.005, 1, True, slip)
        fb = ~m.partial.to_numpy()
        # where the fallback fill sits against the 09:00 open, and the anchored take-profit distance
        ref = np.array([syms[r.sym].open[(r.day + 1) * H_DAY - syms[r.sym].t0] for r in m.itertuples()])
        fill = np.array([syms[r.sym].open[int(r.entry_h) - syms[r.sym].t0] for r in m.itertuples()]) * (1 - slip)
        drop = fill / ref - 1
        anchored = 1 - ref * 1.005 * 0.85 / fill
        if slip == 0.001:
            d = drop[fb]
            out += [f"Fallback entries: {fb.sum()} of {len(m)} main signals. Fill vs 09:00 open: median {pct(np.median(d))}, "
                    f"below −5%: {np.mean(d < -0.05):.0%}, below −10%: {np.mean(d < -0.10):.0%}\n"]
        lt = sleeve_trades("listing", lst, syms, 0.15, 0.15, 2, False)
        lt = [t + (True,) for t in lt]
        variants = {"15% from the fill (now)": np.full(fb.sum(), 0.15)}
        for x in (0.12, 0.10, 0.08):
            variants[f"{x:.0%} from the fill"] = np.full(fb.sum(), x)
        a = np.clip(np.round(anchored[fb], 2), None, 0.15)
        variants["09:00 level (limit's target), skip if under 4% away"] = np.where(a < 0.04, np.nan, a)
        variants["09:00 level, at least 8%"] = np.clip(a, 0.08, 0.15)
        out += [f"### Slippage {slip:.1%}\n",
                "| fallback take-profit | fallback trades | fallback per trade | " + " | ".join(PERIODS) + " |",
                "|---|---|---|" + "---|" * len(PERIODS)]
        for label, tp_fb in variants.items():
            tp_row = np.full(len(m), 0.15)
            tp_row[fb] = tp_fb
            mt = trades_with_tp("main", m, syms, tp_row)
            fbt = [t for t in mt if not t[6]]
            cells = []
            for pname, (a0, b0) in PERIODS.items():
                pa = int(pd.Timestamp(a0).timestamp() // 86400)
                pb = min(int(pd.Timestamp(b0).timestamp() // 86400), last)
                fin, dd, _ = portfolio([t[:6] for t in mt + lt], pa, pb, 0.12, ("main", "listing"))
                cells.append(f"${fin:,.0f} ({pct(dd, 0)})")
            out.append(f"| {label} | {len(fbt)} | {pct(np.mean([t[4] for t in fbt]))} | " + " | ".join(cells) + " |")
            print(out[-1], flush=True)
        out.append("")
    g.SLIP, g.STOP_SLIP = 0.001, 0.005
    print(out[1])
    (HERE / "results" / "fallback_tp.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

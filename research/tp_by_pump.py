"""Take-profit by pump size: does the best take-profit depend on how much the coin rose the day before?

Current rules (L/S 1.5-3.0, 6%/12% by L/S, fallback take-profit at the 09:00 level, stop +15%, 48h, listing 12%).
Only the take-profit changes. Halves: 2024-01..2025-06 and 2025-07..2026-10.
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
from short_study import H_DAY, load, pct, volume_ranks
from stop_and_hold import load_signals
from wick_filter import day_shape

HERE = Path(__file__).resolve().parent
BUCKETS = ((0.2, 0.3), (0.3, 0.5), (0.5, 1.0), (1.0, 99))
TPS = (0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.25, 0.30)


def tp_rows_for(m, syms, slip, tp_of):
    """Per-row take-profit: tp_of(row) for limit fills and fallbacks above the 09:00 open; fallbacks below the
    open aim at the same price level the limit would have used (README 28)."""
    fb = ~m.partial.to_numpy()
    ref = np.array([syms[r.sym].open[(r.day + 1) * H_DAY - syms[r.sym].t0] for r in m.itertuples()])
    fill = np.array([syms[r.sym].open[int(r.entry_h) - syms[r.sym].t0] for r in m.itertuples()]) * (1 - slip)
    base = np.array([tp_of(r) for r in m.itertuples()])
    tp = base.copy()
    below = fb & (fill < ref)
    a = np.round(1 - ref * 1.005 * (1 - base) / fill, 2)
    tp[below] = np.where(a[below] < 0.01, np.nan, np.minimum(np.maximum(a[below], 0.01), base[below]))
    return np.round(tp, 2)


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    da = int(pd.Timestamp("2024-01-01").timestamp() // 86400)
    sig = load_signals(syms, ranks, da, last)
    sig["retrace"] = [day_shape(syms[r.sym], r.day)[1] for r in sig.itertuples()]
    sig["skipf"] = [skip_rule(*funding_at_entry(syms[r.sym], (r.day + 1) * H_DAY)) for r in sig.itertuples()]
    sig = sig[(sig.ls > 1.5) & (sig.ls <= 3.0) & (sig.majors <= 0.07) & (sig.majors >= -0.03) & (sig.retrace <= 0.30)
              & ~sig.skipf].reset_index(drop=True)
    ls = pd.read_csv(HERE / "results" / "listings.csv", parse_dates=["listed"])
    lh = (ls.listed - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(hours=1)
    lst = pd.DataFrame({"sym": ls.perp, "entry_h": lh.astype(int) + 1})
    lst = lst[[s in syms and syms[s].t0 <= h < syms[s].t0 + syms[s].n for s, h in zip(lst.sym, lst.entry_h)]]
    lst["day"] = lst.entry_h // H_DAY - 1
    split = pd.Timestamp("2025-07-01").timestamp() // 86400
    out = ["## Take-profit by pump size (stop +15%, 48h)\n"]
    best = {}
    for slip, ss in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP, g.STOP_SLIP = slip, ss
        m, _ = variant(syms, sig, 0.005, 1, True, slip)
        early = (m.day < split).to_numpy()
        P = g.Paths(syms, m, g.MAX_H)
        cache = {}

        def outcome(tp):
            if tp not in cache:
                cache[tp] = P.outcome(float(tp), 0.15, 2)[0]
            return cache[tp]

        res = {}
        for x in TPS:
            tp = tp_rows_for(m, syms, slip, lambda r, x=x: x)
            ret = np.full(len(m), np.nan)
            for v in sorted(set(tp[~np.isnan(tp)])):
                ret[tp == v] = outcome(v)[tp == v]
            res[x] = ret
        out += [f"### Per signal, slippage {slip:.1%} (early half / late half)\n",
                "| day rise | signals | " + " | ".join(f"TP {x:.0%}" for x in TPS) + " |", "|---|---|" + "---|" * len(TPS)]
        for lo, hi in BUCKETS:
            b = ((m.day_ret >= lo) & (m.day_ret < hi)).to_numpy()
            cells, means = [], {}
            for x in TPS:
                r = res[x]
                e, l = r[b & early & ~np.isnan(r)], r[b & ~early & ~np.isnan(r)]
                means[x] = (e.mean(), l.mean())
                cells.append(f"{pct(e.mean())} / {pct(l.mean())}")
            n = int((b & ~np.isnan(res[0.15])).sum())
            out.append(f"| +{lo:.0%}..{'+' + format(hi, '.0%') if hi < 99 else ''} | {n} | " + " | ".join(cells) + " |")
            if slip == 0.003:
                best[(lo, hi)] = means
        out.append("")
        print("\n".join(out[-len(BUCKETS) - 4:]), flush=True)

    # portfolio: uniform 15% vs a take-profit per bucket chosen where both halves agree (0.3% slippage table)
    pick = {}
    for k, means in best.items():
        ranked = sorted(TPS, key=lambda x: -(means[x][0] + means[x][1]))
        pick[k] = ranked[0]
    print("picked (best on both halves combined, 0.3%):", {f"{k[0]:.0%}+": f"{v:.0%}" for k, v in pick.items()}, flush=True)
    for slip, ss in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP, g.STOP_SLIP = slip, ss
        m, _ = variant(syms, sig, 0.005, 1, True, slip)
        lt = [(t[1], t[2], t[3], t[4], t[5], 0.12) for t in sleeve_trades("listing", lst, syms, 0.15, 0.15, 2, False)]
        size = {(r.sym, int(e)): (0.12 if r.ls > 2.0 else 0.06) for r, e in zip(m.itertuples(), m.entry_h)}

        def bucket_tp(r):
            for (lo, hi), v in pick.items():
                if lo <= r.day_ret < hi:
                    return v
            return 0.15
        variants = {"TP 15% for all (now)": lambda r: 0.15, "TP by pump size": bucket_tp}
        for x in (0.10, 0.20):
            variants[f"TP {x:.0%} for all"] = (lambda r, x=x: x)
        out += [f"### Portfolio, slippage {slip:.1%} ($10k, 6%/12% by L/S, listing 12%)\n",
                "| take-profit | " + " | ".join(WINDOWS) + " |", "|---|" + "---|" * len(WINDOWS)]
        for label, fn in variants.items():
            tp = tp_rows_for(m, syms, slip, fn)
            mt = trades_with_tp("main", m, syms, tp)
            tr = [(t[1], t[2], t[3], t[4], t[5], size[(t[1], t[2])]) for t in mt]
            cells = []
            for a0, b0 in WINDOWS.values():
                pa = int(pd.Timestamp(a0).timestamp() // 86400)
                pb = min(int(pd.Timestamp(b0).timestamp() // 86400), last)
                fin, dd = portfolio_w(tr + lt, pa, pb)
                cells.append(f"${fin:,.0f} ({pct(dd, 0)})")
            out.append(f"| {label} | " + " | ".join(cells) + " |")
            print(out[-1], flush=True)
        out.append("")
    g.SLIP, g.STOP_SLIP = 0.001, 0.005
    out.insert(1, "Picked per bucket: " + ", ".join(f"+{k[0]:.0%}: TP {v:.0%}" for k, v in pick.items()) + "\n")
    (HERE / "results" / "tp_by_pump.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

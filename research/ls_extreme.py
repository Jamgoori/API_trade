"""Very high long/short ratios: per signal by ratio band, and caps on top of the tiered size (README 33)."""

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
BANDS = ((1.5, 2.0), (2.0, 2.5), (2.5, 3.0), (3.0, 4.0), (4.0, 99))


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    da = int(pd.Timestamp("2024-01-01").timestamp() // 86400)
    sig = load_signals(syms, ranks, da, last)
    sig["retrace"] = [day_shape(syms[r.sym], r.day)[1] for r in sig.itertuples()]
    sig["skipf"] = [skip_rule(*funding_at_entry(syms[r.sym], (r.day + 1) * H_DAY)) for r in sig.itertuples()]
    sig = sig[(sig.ls > 1.5) & (sig.majors <= 0.07) & (sig.majors >= -0.03) & (sig.retrace <= 0.30) & ~sig.skipf].reset_index(drop=True)
    ls = pd.read_csv(HERE / "results" / "listings.csv", parse_dates=["listed"])
    lh = (ls.listed - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(hours=1)
    lst = pd.DataFrame({"sym": ls.perp, "entry_h": lh.astype(int) + 1})
    lst = lst[[s in syms and syms[s].t0 <= h < syms[s].t0 + syms[s].n for s, h in zip(lst.sym, lst.entry_h)]]
    lst["day"] = lst.entry_h // H_DAY - 1
    out = ["## Very high long/short ratios\n"]
    for slip, ss in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP, g.STOP_SLIP = slip, ss
        m, _ = variant(syms, sig, 0.005, 1, True, slip)
        tp = tp_rows(m, syms, slip)
        P = g.Paths(syms, m, g.MAX_H)
        ret = np.full(len(m), np.nan)
        for x in sorted(set(tp[~np.isnan(tp)])):
            r = P.outcome(float(x), 0.15, 2)[0]
            ret[tp == x] = r[tp == x]
        year = pd.to_datetime(m.day * 86400, unit="s").dt.year.to_numpy()
        out += [f"### Per signal by L/S band, slippage {slip:.1%}\n",
                "| L/S | 2024 | 2025 | 2026 | all, win |", "|---|---|---|---|---|"]
        for lo, hi in BANDS:
            b = ((m.ls > lo) & (m.ls <= hi)).to_numpy() & ~np.isnan(ret)
            cells = [f"{pct(ret[b & (year == y)].mean()) if (b & (year == y)).sum() else '—'} ({(b & (year == y)).sum()})" for y in (2024, 2025, 2026)]
            out.append(f"| {lo}–{hi if hi < 99 else ''} | " + " | ".join(cells) + f" | {pct(ret[b].mean())} ({b.sum()}), {np.mean(ret[b] > 0):.0%} |")
        out.append("")
        print("\n".join(out[-len(BANDS) - 4:]), flush=True)
        mt = trades_with_tp("main", m, syms, tp)
        ls_of = {(r.sym, int(e)): r.ls for r, e in zip(m.itertuples(), m.entry_h)}
        lt = [(t[1], t[2], t[3], t[4], t[5], 0.12) for t in sleeve_trades("listing", lst, syms, 0.15, 0.15, 2, False)]

        def size_now(r):
            return 0.12 if r > 2.0 else 0.06
        variants = {"tiered (now)": size_now,
                    "tiered, skip above 4.0": lambda r: 0.0 if r > 4.0 else size_now(r),
                    "tiered, skip above 3.0": lambda r: 0.0 if r > 3.0 else size_now(r),
                    "tiered, 6% above 3.0": lambda r: 0.06 if r > 3.0 else size_now(r),
                    "tiered, skip above 2.5": lambda r: 0.0 if r > 2.5 else size_now(r)}
        out += [f"### Portfolio, slippage {slip:.1%} (main + listing, $10k)\n", "| sizing | " + " | ".join(WINDOWS) + " |",
                "|---|" + "---|" * len(WINDOWS)]
        for label, fn in variants.items():
            tr = []
            for t in mt:
                f = fn(ls_of.get((t[1], t[2]), np.nan))
                if f > 0:
                    tr.append((t[1], t[2], t[3], t[4], t[5], f))
            cells = []
            for a0, b0 in WINDOWS.values():
                pa = int(pd.Timestamp(a0).timestamp() // 86400)
                pb = min(int(pd.Timestamp(b0).timestamp() // 86400), last)
                fin, dd = portfolio_w(tr + lt, pa, pb)
                yrs = (pb - pa + 1) / 365.25
                cells.append(f"${fin:,.0f} ({pct(dd, 0)}, {pct((fin / 10000) ** (1 / yrs) - 1, 0)}/yr)")
            out.append(f"| {label} | " + " | ".join(cells) + " |")
            print(out[-1], flush=True)
        out.append("")
    g.SLIP, g.STOP_SLIP = 0.001, 0.005
    (HERE / "results" / "ls_extreme.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

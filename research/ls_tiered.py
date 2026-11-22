"""Tiered size by long/short ratio: L/S 1.5-2.0 signals at a smaller size, above 2.0 at 12%.

Main rule as in STRATEGY.md incl. the fallback take-profit (README 28), plus the listing sleeve at 12%, $10k.
Margin committed stays within 90% of equity.
"""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from combined import sleeve_trades
from fallback_tp import trades_with_tp
from funding_filter import funding_at_entry, skip_rule
from limit_entry import variant
from majors_down import tp_rows
from short_study import H_DAY, load, pct, volume_ranks
from stop_and_hold import load_signals
from wick_filter import day_shape

HERE = Path(__file__).resolve().parent
WINDOWS = {"2024-01..2026-10": ("2024-01-01", "2026-10-31"), "2025-01..2026-10": ("2025-01-01", "2026-10-31"),
           "2026-01..2026-10": ("2026-01-01", "2026-10-31")}


def portfolio_w(trades, day_a, day_b):
    """trades: (sym, entry_h, exit_h, ret, mtm, frac). Equity marked daily; skip a trade if it would push margin
    over 90% of equity or the coin is already held."""
    trades = sorted([t for t in trades if day_a * H_DAY <= t[1] < (day_b + 1) * H_DAY], key=lambda t: t[1])
    by_day = {}
    for t in trades:
        by_day.setdefault(t[1] // H_DAY, []).append(t)
    cash, open_, curve = 1.0, [], []

    def val(p, d):
        if p["exit_day"] <= d:
            return p["ret"]
        k = d - p["entry_day"]
        return p["mtm"][k] if 0 <= k < len(p["mtm"]) and not np.isnan(p["mtm"][k]) else 0.0

    for day in range(day_a, day_b + 1):
        keep = []
        for p in open_:
            if p["exit_day"] < day:
                cash += p["m"] * (1 + p["ret"])
            else:
                keep.append(p)
        open_ = keep
        equity = cash + sum(p["m"] * (1 + val(p, day - 1)) for p in open_)
        for t in by_day.get(day, []):
            m = equity * t[5]
            if sum(p["m"] for p in open_) + m > 0.9 * equity * (1 + 1e-9) or any(p["sym"] == t[0] for p in open_):
                continue
            cash -= m
            open_.append({"m": m, "sym": t[0], "ret": t[3], "mtm": t[4], "entry_day": t[1] // H_DAY, "exit_day": t[2] // H_DAY})
        curve.append(cash + sum(p["m"] * (1 + val(p, day)) for p in open_))
    eq = np.array(curve)
    return eq[-1] * 10_000, float((eq / np.maximum.accumulate(eq) - 1).min())


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
    out = ["## Tiered size by long/short ratio (main + listing, $10k)\n"]
    for slip, ss in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP, g.STOP_SLIP = slip, ss
        m, _ = variant(syms, sig, 0.005, 1, True, slip)
        tp = tp_rows(m, syms, slip)
        # one simulation of all main trades (one per coin, drop after stop), tagged with their L/S ratio
        mt = trades_with_tp("main", m, syms, tp)
        ls_of = {(r.sym, int(e)): r.ls for r, e in zip(m.itertuples(), m.entry_h)}
        lt = [(t[1], t[2], t[3], t[4], t[5], 0.12) for t in sleeve_trades("listing", lst, syms, 0.15, 0.15, 2, False)]
        variants = {"1.5+ all at 12% (now)": (0.12, 0.12), "2.0+ only at 12%": (0.0, 0.12),
                    "1.5-2.0 at 4%, 2.0+ at 12%": (0.04, 0.12), "1.5-2.0 at 6%, 2.0+ at 12%": (0.06, 0.12),
                    "1.5-2.0 at 8%, 2.0+ at 12%": (0.08, 0.12)}
        out += [f"### Slippage {slip:.1%}\n", "| sizing | " + " | ".join(WINDOWS) + " |", "|---|" + "---|" * len(WINDOWS)]
        for label, (lo, hi) in variants.items():
            tr = []
            for t in mt:
                ratio = ls_of.get((t[1], t[2]), np.nan)
                f = hi if ratio > 2.0 else lo
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
    (HERE / "results" / "ls_tiered.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

"""Long/short ratio threshold for the main rule: 1.5 (now), 1.75, 2.0, 2.25.

Everything else as in STRATEGY.md incl. the 10:00 KST fallback take-profit (README 28); main + listing, 12%, $10k.
"""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from combined import PERIODS, portfolio, sleeve_trades
from fallback_tp import trades_with_tp
from funding_filter import funding_at_entry, skip_rule
from limit_entry import variant
from majors_down import tp_rows
from short_study import H_DAY, load, pct, volume_ranks
from stop_and_hold import load_signals
from wick_filter import day_shape

HERE = Path(__file__).resolve().parent
THRESHOLDS = (1.5, 1.75, 2.0, 2.25)


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
    months = {2024: 12, 2025: 12, 2026: 10}
    out = ["## Long/short ratio threshold (main + listing, 12%, $10k)\n"]
    for slip, ss in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP, g.STOP_SLIP = slip, ss
        m, _ = variant(syms, sig, 0.005, 1, True, slip)
        tp = tp_rows(m, syms, slip)
        lt = sleeve_trades("listing", lst, syms, 0.15, 0.15, 2, False)
        year = pd.to_datetime(m.day * 86400, unit="s").dt.year.to_numpy()
        out += [f"### Slippage {slip:.1%}\n",
                "| L/S above | main trades taken (per month) | per trade 2024 / 2025 / 2026 (trades) | win | "
                + " | ".join(PERIODS) + " |", "|---|---|---|---|" + "---|" * len(PERIODS)]
        for th in THRESHOLDS:
            keep = (m.ls > th).to_numpy()
            mt = trades_with_tp("main", m[keep].reset_index(drop=True), syms, tp[keep])
            ty = pd.to_datetime(np.array([t[2] for t in mt]) * 3600, unit="s").year
            rets = np.array([t[4] for t in mt])
            by_year = " / ".join(f"{pct(rets[ty == y].mean())} ({(ty == y).sum()})" for y in months)
            cells = []
            for pname, (a0, b0) in PERIODS.items():
                pa = int(pd.Timestamp(a0).timestamp() // 86400)
                pb = min(int(pd.Timestamp(b0).timestamp() // 86400), last)
                fin, dd, _ = portfolio([t[:6] for t in mt] + lt, pa, pb, 0.12, ("main", "listing"))
                cells.append(f"${fin:,.0f} ({pct(dd, 0)})")
            out.append(f"| {th:g}{' (now)' if th == 1.5 else ''} | {len(mt)} ({len(mt) / 34:.1f}) | {by_year} | "
                       f"{np.mean(rets > 0):.0%} | " + " | ".join(cells) + " |")
            print(out[-1], flush=True)
        out.append("")
    g.SLIP, g.STOP_SLIP = 0.001, 0.005
    (HERE / "results" / "ls_threshold.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

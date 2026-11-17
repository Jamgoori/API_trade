"""Would coins inside the top 100 by volume (BLESS rank 91 +108%, HUMA rank 88 +50% on 2026-11-16) have paid?

Same pipeline as the 12% table in STRATEGY.md (combined.py): +20% daily close, L/S ratio > 1.5, majors −3%..+7%,
wick ≤ 30%, funding floor, +0.5% limit then 01:00 UTC fallback, TP −15% / stop +15% / 48h, drop after a stop,
plus the listing sleeve, 12% margin, max 90% committed. Only the volume-rank rule changes.
"""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from combined import PERIODS, portfolio, sleeve_trades
from funding_filter import funding_at_entry, skip_rule
from limit_entry import variant
from short_study import H_DAY, load, pct, volume_ranks
from stop_and_hold import load_signals
from wick_filter import day_shape

HERE = Path(__file__).resolve().parent
RULES = {
    "outside top 100 (now)": lambda s: s["rank"] > 100,
    "outside top 70": lambda s: s["rank"] > 70,
    "outside top 50": lambda s: s["rank"] > 50,
    "any rank": lambda s: s["rank"] > 0,
    "outside top 100, or top 51–100 with +50% day": lambda s: (s["rank"] > 100) | ((s["rank"] > 50) & (s.day_ret >= 0.5)),
    "outside top 100, or any rank with +50% day": lambda s: (s["rank"] > 100) | (s.day_ret >= 0.5),
    "outside top 100, or top 51–100 with +40% day": lambda s: (s["rank"] > 100) | ((s["rank"] > 50) & (s.day_ret >= 0.4)),
}


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    da = int(pd.Timestamp("2024-01-01").timestamp() // 86400)
    sig = load_signals(syms, ranks, da, last, min_rank=0)
    sig["retrace"] = [day_shape(syms[r.sym], r.day)[1] for r in sig.itertuples()]
    sig["skipf"] = [skip_rule(*funding_at_entry(syms[r.sym], (r.day + 1) * H_DAY)) for r in sig.itertuples()]
    sig = sig[(sig.ls > 1.5) & (sig.majors <= 0.07) & (sig.majors >= -0.03) & (sig.retrace <= 0.30) & ~sig.skipf]
    sig = sig.reset_index(drop=True)

    ls = pd.read_csv(HERE / "results" / "listings.csv", parse_dates=["listed"])
    hours = (ls.listed - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(hours=1)
    lst = pd.DataFrame({"sym": ls.perp, "entry_h": hours.astype(int) + 1})
    lst = lst[[s in syms and syms[s].t0 <= h < syms[s].t0 + syms[s].n for s, h in zip(lst.sym, lst.entry_h)]]
    lst["day"] = lst.entry_h // H_DAY - 1

    out = ["## Volume-rank rule variants (main + listing, 12% per trade, $10k)\n",
           "Per cell: final $ (max drawdown). Main per trade = average return on margin of the main-rule trades taken "
           "on their own (one per coin, drop after stop).\n"]
    # per-signal quality by band (all filters, limit entry, 0.3% slippage)
    g.SLIP, g.STOP_SLIP = 0.003, 0.01
    s3, _ = variant(syms, sig, 0.005, 1, True, 0.003)
    P = g.Paths(syms, s3, g.MAX_H)
    ret = P.outcome(0.15, 0.15, 2)[0]
    split = pd.Timestamp("2025-07-01").timestamp() // 86400
    out += ["### Per signal by band (0.3% slippage, before one-per-coin and drop rules)\n",
            "| band | 2024-01..2025-06 | 2025-07..2026-10 |", "|---|---|---|"]
    bands = [("rank 1–50, +20–50%", (s3["rank"] <= 50) & (s3.day_ret < 0.5)),
             ("rank 1–50, +50% or more", (s3["rank"] <= 50) & (s3.day_ret >= 0.5)),
             ("rank 51–100, +20–50%", (s3["rank"] > 50) & (s3["rank"] <= 100) & (s3.day_ret < 0.5)),
             ("rank 51–100, +50% or more", (s3["rank"] > 50) & (s3["rank"] <= 100) & (s3.day_ret >= 0.5)),
             ("rank 101+, +20–50%", (s3["rank"] > 100) & (s3.day_ret < 0.5)),
             ("rank 101+, +50% or more", (s3["rank"] > 100) & (s3.day_ret >= 0.5))]
    for name, m in bands:
        cells = []
        for early in (True, False):
            sel = m.to_numpy() & ((s3.day < split) if early else (s3.day >= split)).to_numpy() & ~np.isnan(ret)
            cells.append(f"{sel.sum()}, {pct(np.mean(ret[sel]))}, win {np.mean(ret[sel] > 0):.0%}" if sel.any() else "—")
        out.append(f"| {name} | " + " | ".join(cells) + " |")
        print(out[-1], flush=True)
    out.append("")

    for slip, stop_slip in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP, g.STOP_SLIP = slip, stop_slip
        main_all, _ = variant(syms, sig, 0.005, 1, True, slip)
        listing = sleeve_trades("listing", lst, syms, 0.15, 0.15, 2, False)
        out += [f"### Slippage {slip:.1%} (stop {stop_slip:.1%})\n",
                "| rank rule | main trades | main per trade | " + " | ".join(PERIODS) + " |",
                "|---|---|---|" + "---|" * len(PERIODS)]
        for label, rule in RULES.items():
            ms = main_all[rule(main_all).to_numpy()].reset_index(drop=True)
            mt = sleeve_trades("main", ms, syms, 0.15, 0.15, 2, True)
            cells = []
            for pname, (a, b) in PERIODS.items():
                pa = int(pd.Timestamp(a).timestamp() // 86400)
                pb = min(int(pd.Timestamp(b).timestamp() // 86400), last)
                fin, dd, tk = portfolio(mt + listing, pa, pb, 0.12, ("main", "listing"))
                cells.append(f"${fin:,.0f} ({pct(dd, 0)})")
            out.append(f"| {label} | {len(mt)} | {pct(np.mean([t[4] for t in mt]))} | " + " | ".join(cells) + " |")
            print(out[-1], flush=True)
        out.append("")
    g.SLIP, g.STOP_SLIP = 0.001, 0.005
    (HERE / "results" / "rank_override.md").write_text("\n".join(out) + "\n")




def big_pumps() -> None:
    """BLESS / HUMA also failed the L/S ratio rule (0.68 / 0.88). What if big pumps skip it?"""
    import sys
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    da = int(pd.Timestamp("2024-01-01").timestamp() // 86400)
    sig = load_signals(syms, ranks, da, last, min_rank=0)
    sig["retrace"] = [day_shape(syms[r.sym], r.day)[1] for r in sig.itertuples()]
    sig["skipf"] = [skip_rule(*funding_at_entry(syms[r.sym], (r.day + 1) * H_DAY)) for r in sig.itertuples()]
    sig = sig[(sig.majors <= 0.07) & (sig.majors >= -0.03) & (sig.retrace <= 0.30) & ~sig.skipf].reset_index(drop=True)
    rules = {
        "now: outside top 100 and L/S > 1.5": lambda f: (f["rank"] > 100) & (f.ls > 1.5),
        "+ any +50% day outside top 50, L/S ignored":
            lambda f: ((f["rank"] > 100) & (f.ls > 1.5)) | ((f["rank"] > 50) & (f.day_ret >= 0.5)),
        "+ any +50% day, any rank, L/S ignored": lambda f: ((f["rank"] > 100) & (f.ls > 1.5)) | (f.day_ret >= 0.5),
        "+ any +100% day, any rank, L/S ignored": lambda f: ((f["rank"] > 100) & (f.ls > 1.5)) | (f.day_ret >= 1.0),
    }
    ls = pd.read_csv(HERE / "results" / "listings.csv", parse_dates=["listed"])
    hours = (ls.listed - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(hours=1)
    lst = pd.DataFrame({"sym": ls.perp, "entry_h": hours.astype(int) + 1})
    lst = lst[[s in syms and syms[s].t0 <= h < syms[s].t0 + syms[s].n for s, h in zip(lst.sym, lst.entry_h)]]
    lst["day"] = lst.entry_h // H_DAY - 1
    out = ["## Big pumps that fail the L/S ratio rule\n"]
    g.SLIP, g.STOP_SLIP = 0.003, 0.01
    s3, _ = variant(syms, sig, 0.005, 1, True, 0.003)
    ret = g.Paths(syms, s3, g.MAX_H).outcome(0.15, 0.15, 2)[0]
    split = pd.Timestamp("2025-07-01").timestamp() // 86400
    out += ["### Per signal, L/S ratio ≤ 1.5 (0.3% slippage)\n",
            "| band | 2024-01..2025-06 | 2025-07..2026-10 |", "|---|---|---|"]
    low = (s3.ls <= 1.5) | s3.ls.isna()
    for name, m in (("+50–100% day, rank 51+", low & (s3.day_ret >= 0.5) & (s3.day_ret < 1.0) & (s3["rank"] > 50)),
                    ("+100% day or more, rank 51+", low & (s3.day_ret >= 1.0) & (s3["rank"] > 50)),
                    ("+50% day or more, rank 1–50", low & (s3.day_ret >= 0.5) & (s3["rank"] <= 50))):
        cells = []
        for early in (True, False):
            sel = m.to_numpy() & ((s3.day < split) if early else (s3.day >= split)).to_numpy() & ~np.isnan(ret)
            cells.append(f"{sel.sum()}, {pct(np.mean(ret[sel]))}, win {np.mean(ret[sel] > 0):.0%}" if sel.any() else "—")
        out.append(f"| {name} | " + " | ".join(cells) + " |")
        print(out[-1], flush=True)
    out.append("")
    for slip, stop_slip in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP, g.STOP_SLIP = slip, stop_slip
        main_all, _ = variant(syms, sig, 0.005, 1, True, slip)
        listing = sleeve_trades("listing", lst, syms, 0.15, 0.15, 2, False)
        out += [f"### Slippage {slip:.1%} (stop {stop_slip:.1%})\n",
                "| rule | main trades | main per trade | " + " | ".join(PERIODS) + " |", "|---|---|---|" + "---|" * len(PERIODS)]
        for label, rule in rules.items():
            mt = sleeve_trades("main", main_all[rule(main_all).to_numpy()].reset_index(drop=True), syms, 0.15, 0.15, 2, True)
            cells = []
            for pname, (a, b) in PERIODS.items():
                pa = int(pd.Timestamp(a).timestamp() // 86400)
                pb = min(int(pd.Timestamp(b).timestamp() // 86400), last)
                fin, dd, _ = portfolio(mt + listing, pa, pb, 0.12, ("main", "listing"))
                cells.append(f"${fin:,.0f} ({pct(dd, 0)})")
            out.append(f"| {label} | {len(mt)} | {pct(np.mean([t[4] for t in mt]))} | " + " | ".join(cells) + " |")
            print(out[-1], flush=True)
        out.append("")
    g.SLIP, g.STOP_SLIP = 0.001, 0.005
    with open(HERE / "results" / "rank_override.md", "a") as fh:
        fh.write("\n".join(out) + "\n")


if __name__ == "__main__":
    import sys
    big_pumps() if "--big" in sys.argv else main()

"""Does a two-day run-up matter? (a) the current +20% signals split by the move of the day before, and
(b) adding coins whose two-day rise is large although the last day alone is under +20%.

All other rules as in STRATEGY.md: outside the top 100 by volume, L/S ratio > 1.5, majors −3%..+7%, wick ≤ 30%,
funding floor, +0.5% limit then 01:00 UTC fallback, TP −15% / stop +15% / 48h, drop after stop, listing sleeve,
12% margin.
"""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from combined import PERIODS, portfolio, sleeve_trades
from farm_filter import fetch_ls
from funding_filter import funding_at_entry, skip_rule
from limit_entry import variant
from majors_filter import majors_day_return
from pump_day_short import daily_close_index
from short_study import H_DAY, load, pct, volume_ranks
from wick_filter import day_shape

HERE = Path(__file__).resolve().parent


def candidates(syms, ranks, da, db):
    rows = []
    for name, s in syms.items():
        for day in range(max(da, s.t0 // H_DAY + 3), db):
            k, k1, k2 = (daily_close_index(s, d) for d in (day, day - 1, day - 2))
            if k is None or k1 is None or k2 is None:
                continue
            r1 = s.close[k] / s.close[k1] - 1
            prev = s.close[k1] / s.close[k2] - 1
            r2 = s.close[k] / s.close[k2] - 1
            if (r1 >= 0.20 or r2 >= 0.30) and day in ranks.index and ranks.at[day, name] > 100:
                rows.append({"sym": name, "day": day, "day_ret": r1, "prev_ret": prev, "two_day": r2})
    sig = pd.DataFrame(rows).sort_values("day").reset_index(drop=True)
    sig["rank"] = [ranks.at[d, s] for s, d in zip(sig.sym, sig.day)]
    ls = fetch_ls([(r.sym, (r.day + 1) * 86400) for r in sig.itertuples()])
    sig["ls"] = [ls.get((r.sym, (r.day + 1) * 86400), np.nan) for r in sig.itertuples()]
    sig["majors"] = [majors_day_return(syms, d) for d in sig.day]
    sig["retrace"] = [day_shape(syms[r.sym], r.day)[1] for r in sig.itertuples()]
    sig["skipf"] = [skip_rule(*funding_at_entry(syms[r.sym], (r.day + 1) * H_DAY)) for r in sig.itertuples()]
    ok = (sig.ls > 1.5) & (sig.majors <= 0.07) & (sig.majors >= -0.03) & (sig.retrace <= 0.30) & ~sig.skipf
    return sig[ok].reset_index(drop=True)


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    da = int(pd.Timestamp("2024-01-01").timestamp() // 86400)
    sig = candidates(syms, ranks, da, last)
    print(f"candidates after filters: {len(sig)} (day +20%: {(sig.day_ret >= 0.2).sum()})", flush=True)
    split = pd.Timestamp("2025-07-01").timestamp() // 86400
    ls = pd.read_csv(HERE / "results" / "listings.csv", parse_dates=["listed"])
    lh = (ls.listed - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(hours=1)
    lst = pd.DataFrame({"sym": ls.perp, "entry_h": lh.astype(int) + 1})
    lst = lst[[s in syms and syms[s].t0 <= h < syms[s].t0 + syms[s].n for s, h in zip(lst.sym, lst.entry_h)]]
    lst["day"] = lst.entry_h // H_DAY - 1
    out = ["## Two-day run-ups (12%, $10k, main + listing)\n"]
    for slip, ss in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP, g.STOP_SLIP = slip, ss
        m, _ = variant(syms, sig, 0.005, 1, True, slip)
        ret = g.Paths(syms, m, g.MAX_H).outcome(0.15, 0.15, 2)[0]
        out += [f"### Per signal, slippage {slip:.1%} (before one-per-coin and drop rules)\n",
                "| group | 2024-01..2025-06 | 2025-07..2026-10 |", "|---|---|---|"]
        groups = [
            ("now (+20% day): day before fell", (m.day_ret >= 0.2) & (m.prev_ret < 0)),
            ("now (+20% day): day before 0..+10%", (m.day_ret >= 0.2) & (m.prev_ret >= 0) & (m.prev_ret < 0.1)),
            ("now (+20% day): day before +10..+20%", (m.day_ret >= 0.2) & (m.prev_ret >= 0.1) & (m.prev_ret < 0.2)),
            ("now (+20% day): day before +20% or more", (m.day_ret >= 0.2) & (m.prev_ret >= 0.2)),
            ("new: day < +20%, two days +30..50%", (m.day_ret < 0.2) & (m.two_day >= 0.3) & (m.two_day < 0.5)),
            ("new: day < +20%, two days +50% or more", (m.day_ret < 0.2) & (m.two_day >= 0.5)),
        ]
        for name, gm in groups:
            cells = []
            for early in (True, False):
                sel = gm.to_numpy() & ((m.day < split) if early else (m.day >= split)).to_numpy() & ~np.isnan(ret)
                cells.append(f"{sel.sum()}, {pct(np.mean(ret[sel]))}, win {np.mean(ret[sel] > 0):.0%}" if sel.any() else "—")
            out.append(f"| {name} | " + " | ".join(cells) + " |")
            print(out[-1], flush=True)
        out.append("")
        lt = sleeve_trades("listing", lst, syms, 0.15, 0.15, 2, False)
        rules = {
            "now: day +20%": m.day_ret >= 0.2,
            "+ two days +30% (day under +20%)": (m.day_ret >= 0.2) | (m.two_day >= 0.3),
            "+ two days +50% (day under +20%)": (m.day_ret >= 0.2) | (m.two_day >= 0.5),
            "now, but skip if the day before was +20% too": (m.day_ret >= 0.2) & (m.prev_ret < 0.2),
            "now, but skip if the day before was +10..20%": (m.day_ret >= 0.2) & ~((m.prev_ret >= 0.1) & (m.prev_ret < 0.2)),
            "now, but skip if the day before was +10% or more": (m.day_ret >= 0.2) & (m.prev_ret < 0.1),
        }
        out += [f"### Portfolio, slippage {slip:.1%}\n", "| rule | main trades | " + " | ".join(PERIODS) + " |",
                "|---|---|" + "---|" * len(PERIODS)]
        for label, rm in rules.items():
            mt = sleeve_trades("main", m[rm.to_numpy()].reset_index(drop=True), syms, 0.15, 0.15, 2, True)
            cells = []
            for pname, (a, b) in PERIODS.items():
                pa = int(pd.Timestamp(a).timestamp() // 86400)
                pb = min(int(pd.Timestamp(b).timestamp() // 86400), last)
                fin, dd, _ = portfolio(mt + lt, pa, pb, 0.12, ("main", "listing"))
                cells.append(f"${fin:,.0f} ({pct(dd, 0)})")
            out.append(f"| {label} | {len(mt)} | " + " | ".join(cells) + " |")
            print(out[-1], flush=True)
        out.append("")
    g.SLIP, g.STOP_SLIP = 0.001, 0.005
    (HERE / "results" / "two_day_pump.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

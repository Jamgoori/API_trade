"""Two follow-ups to tp_grid.py.

1. No time limit: hold until the take-profit or the stop (capped at 180 days, open trades valued at the end
   of the data) versus fixed 3/7/30-day holds.
2. Position size for a $10k account: margin per trade and leverage, for the two combinations that held up in
   both periods. Leverage is only tested where the liquidation price stays well above the stop.
"""

from __future__ import annotations

import itertools
from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from farm_filter import fetch_ls
from pump_day_short import signals
from short_study import H_DAY, load, pct, volume_ranks

CAP_H = 180 * H_DAY
PERIODS = {"2022-24": ("2022-01-01", "2024-12-31"), "2025-26": ("2025-01-01", "2026-10-31")}
HERE = Path(__file__).resolve().parent


def hold_name(h):
    return "until TP/stop" if h is None else f"{h}d"


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    data = {}
    for pname, (a, b) in PERIODS.items():
        da = int(pd.Timestamp(a).timestamp() // 86400)
        db = min(int(pd.Timestamp(b).timestamp() // 86400), last)
        for trig in (0.20, 0.30):
            sig = signals(syms, ranks, trig, da, db).sort_values("day").reset_index(drop=True)
            ls = fetch_ls([(r.sym, (r.day + 1) * 86400) for r in sig.itertuples()])
            sig["ls"] = [ls.get((r.sym, (r.day + 1) * 86400), np.nan) for r in sig.itertuples()]
            data[(pname, trig)] = (sig, g.Paths(syms, sig, CAP_H), da, db)
    out = []

    # 1. holding period, including no time limit
    rows = []
    for trig, ratio, tp, stop, ban, hold in itertools.product((0.20, 0.30), (None, 1.5), (0.10, 0.15, 0.20),
                                                             (0.15, 0.20, 0.30), (False, True), (3, 7, 30, None)):
        row = {"trigger": trig, "ratio": ratio or 0, "tp": tp, "stop": stop, "ban": ban, "hold": hold_name(hold)}
        for pname in PERIODS:
            sig, P, da, db = data[(pname, trig)]
            mask = np.ones(len(sig), bool) if ratio is None else (sig.ls > ratio).to_numpy()
            r = g.run_combo(sig, P, mask, tp, stop, hold, ban, da, db, truncate=True)
            row |= {f"{k}_{pname}": v for k, v in r.items()}
        rows.append(row)
    h = pd.DataFrame(rows)
    h.to_csv(HERE / "results" / "tp_hold.csv", index=False)
    out.append("## 1. Holding period: fixed days versus no time limit (2% margin per trade, 1x)\n")
    out.append("Median over all 72 combinations of trigger, ratio filter, take-profit, stop and drop-after-stop.\n")
    out.append("| hold | 2022-24 CAGR | 2022-24 max DD | 2025-26 CAGR | 2025-26 max DD | combos with CAGR > 0 in both |")
    out.append("|---|---|---|---|---|---|")
    for hn, x in h.groupby("hold", sort=False):
        both = ((x["cagr_2022-24"] > 0) & (x["cagr_2025-26"] > 0)).mean()
        out.append(f"| {hn} | {pct(x['cagr_2022-24'].median())} | {pct(x['maxdd_2022-24'].median(), 0)} "
                   f"| {pct(x['cagr_2025-26'].median())} | {pct(x['maxdd_2025-26'].median(), 0)} | {both:.0%} |")
    out.append("")

    # 2. position size and leverage for the two combinations that held up in both periods
    combos = {
        "A: +20% day, ratio>1.5, TP −15%, stop +15%, drop after stop": (0.20, 1.5, 0.15, 0.15, True),
        "B: +30% day, TP −10%, stop +30%": (0.30, None, 0.10, 0.30, False),
    }
    out.append("## 2. Position size for a $10k account\n")
    out.append("Margin per trade as a share of equity; at most 1/margin positions open (new signals are skipped "
               "when the margin is used up). Notional = margin × leverage. $10k column: 2025-01 to 2026-10.\n")
    out.append("| combination | hold | margin / trade | leverage | notional / trade | 2022-24 CAGR | max DD | 2025-26 CAGR | max DD | $10k → |")
    out.append("|---|---|---|---|---|---|---|---|---|---|")
    for name, (trig, ratio, tp, stop, ban) in combos.items():
        levs = (1.0, 2.0, 3.0) if stop <= 0.15 else (1.0, 2.0)
        for hold in (3, None):
            for lev in levs:
                for frac in (0.05, 0.10, 0.15, 0.20, 0.25, 0.33, 0.50):
                    cells = []
                    for pname in PERIODS:
                        sig, P, da, db = data[(pname, trig)]
                        mask = np.ones(len(sig), bool) if ratio is None else (sig.ls > ratio).to_numpy()
                        cells.append(g.run_combo(sig, P, mask, tp, stop, hold, ban, da, db,
                                                 margin_frac=frac, lev=lev, truncate=True))
                    a, b = cells
                    years_b = (data[("2025-26", trig)][3] - data[("2025-26", trig)][2] + 1) / 365
                    final = 10_000 * (1 + b["cagr"]) ** years_b
                    out.append(f"| {name[:1]} | {hold_name(hold)} | {frac:.0%} | {lev:.0f}x | {frac * lev:.0%} "
                               f"| {pct(a['cagr'], 0)} | {pct(a['maxdd'], 0)} | {pct(b['cagr'], 0)} | {pct(b['maxdd'], 0)} "
                               f"| ${final:,.0f} |")
                    print(out[-1], flush=True)
    out.insert(len(out) - 0, "")
    text = "\n".join(out) + "\n\nCombinations:\n\n" + "\n".join(f"- {k}" for k in combos) + "\n"
    (HERE / "results" / "tp_sizing.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()

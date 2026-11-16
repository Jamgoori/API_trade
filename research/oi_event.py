"""OI event short with the current exits, checked on two periods.

Event (from short_study.py): 4h return >= +20% and the hour's volume >= 5x its 7-day average.
Filters at the signal: open interest up 50%+ over 4h and global long/short account ratio > 1.5.
Entry at the next hour's open. Exits: take-profit −15%, stop +15%, 48h, drop a coin after a stop (as STRATEGY.md),
and the original event-study exits (+30% stop, 7 days) for comparison. 10% margin, $10k.
"""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from short_study import H_DAY, load, pct, volume_ranks

HERE = Path(__file__).resolve().parent
PERIODS = {"2024-01..2025-06": ("2024-01-01", "2025-06-30"), "2025-07..2026-10": ("2025-07-01", "2026-10-31"),
           "2024-01..2026-10": ("2024-01-01", "2026-10-31")}


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    ev = pd.read_csv(HERE / "results" / "events_oi.csv").dropna(subset=["oi_4h", "ls_accounts"])
    ev["entry_h"] = ev.abs_h + 1
    ev["day"] = ev.entry_h // H_DAY - 1
    rules = {
        "OI 4h > +50% & ratio > 1.5": (ev.oi_4h > 0.5) & (ev.ls_accounts > 1.5),
        "same, volume rank > 100 only": (ev.oi_4h > 0.5) & (ev.ls_accounts > 1.5) & (ev["rank"] > 100),
        "ratio > 1.5 only": ev.ls_accounts > 1.5,
    }
    exits = {"TP −15%, stop +15%, 48h, drop after stop": (0.15, 0.15, 2, True),
             "original: TP −15%, stop +30%, 7d": (0.15, 0.30, 7, False)}
    out = ["## OI event short (entry at the next hour's open, 10% margin, $10k)\n",
           "Per cell: trades, per trade, $10k → (max DD).\n",
           "| filter | exits | slippage | " + " | ".join(PERIODS) + " |", "|---|---|---|" + "---|" * len(PERIODS)]
    for rname, rmask in rules.items():
        for ename, (tp, stop, hold, ban) in exits.items():
            for slip, stop_slip in ((0.001, 0.005), (0.003, 0.01)):
                g.SLIP, g.STOP_SLIP = slip, stop_slip
                cells = []
                for pname, (a, b) in PERIODS.items():
                    da = int(pd.Timestamp(a).timestamp() // 86400)
                    db = min(int(pd.Timestamp(b).timestamp() // 86400), last)
                    sel = ev[rmask & (ev.entry_h >= da * H_DAY) & (ev.entry_h < (db + 1) * H_DAY)]
                    sig = sel.sort_values("entry_h").reset_index(drop=True)
                    if sig.empty:
                        cells.append("—")
                        continue
                    P = g.Paths(syms, sig, g.MAX_H)
                    r = g.run_combo(sig, P, np.ones(len(sig), bool), tp, stop, hold, ban, da, db,
                                    margin_frac=0.10, return_curve=True)
                    cells.append("—" if r is None else
                                 f"{r['trades']}, {pct(r['mean'])}, ${r['curve'][-1] * 10_000:,.0f} ({pct(r['maxdd'], 0)})")
                out.append(f"| {rname} | {ename} | {slip:.1%} | " + " | ".join(cells) + " |")
                print(out[-1], flush=True)
    g.SLIP, g.STOP_SLIP = 0.001, 0.005
    (HERE / "results" / "oi_event.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

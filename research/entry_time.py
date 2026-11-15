"""Best time of day to enter.

A. Which UTC hour to run the once-a-day check (close at that hour >= +20% above the close 24h earlier,
   majors and long/short filters at that hour, entry at that hour's open).
B. Keep the 00:00 UTC check, but wait d hours before entering (filters judged at 00:00).
Exits as in STRATEGY.md (TP −15%, stop +15%, 48h, drop after stop, 1x), 10% margin per trade.
"""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from check_interval import majors_24h, rolling_signals
from farm_filter import fetch_ls
from short_study import H_DAY, load, pct, volume_ranks

HERE = Path(__file__).resolve().parent
PERIODS = {"2024-01..2025-06": ("2024-01-01", "2025-06-30"), "2025-07..2026-10": ("2025-07-01", "2026-10-31"),
           "2024-01..2026-10": ("2024-01-01", "2026-10-31")}


def run(syms, sig, mask, da, db):
    cells = []
    for slip, stop_slip in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP, g.STOP_SLIP = slip, stop_slip
        P = g.Paths(syms, sig, g.MAX_H)
        r = g.run_combo(sig, P, mask, 0.15, 0.15, 2, True, da, db, margin_frac=0.10, return_curve=True)
        cells.append(r)
    g.SLIP, g.STOP_SLIP = 0.001, 0.005
    return cells


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    base = {}
    for pname, (a, b) in PERIODS.items():
        da = int(pd.Timestamp(a).timestamp() // 86400)
        db = min(int(pd.Timestamp(b).timestamp() // 86400), last)
        base[pname] = (da, db)

    def table(title, rows_fn, labels):
        out = [f"### {title}\n", "Per cell: trades, per trade, $10k → (max DD); last column = whole period with 0.3% slippage.\n",
               "| setting | " + " | ".join(PERIODS) + " | whole period, 0.3% slippage |", "|---|" + "---|" * (len(PERIODS) + 1)]
        for lab in labels:
            cells, slow = [], None
            for pname, (da, db) in base.items():
                sig, mask = rows_fn(lab, da, db)
                r1, r3 = run(syms, sig, mask, da, db)
                cells.append(f"{r1['trades']}, {pct(r1['mean'])}, ${r1['curve'][-1] * 10_000:,.0f} ({pct(r1['maxdd'], 0)})")
                slow = r3
            out.append(f"| {lab} | " + " | ".join(cells) + f" | ${slow['curve'][-1] * 10_000:,.0f} ({pct(slow['maxdd'], 0)}) |")
            print(out[-1], flush=True)
        return out

    cache = {}

    def check_hour(lab, da, db):
        hour = int(lab.split(":")[0])
        key = ("A", hour, da, db)
        if key not in cache:
            sig = rolling_signals(syms, ranks, da * H_DAY, (db + 1) * H_DAY, 24, offset=hour)
            sig = sig.sort_values("entry_h").reset_index(drop=True)
            sig["majors"] = [majors_24h(syms, int(h)) for h in sig.entry_h]
            ls = fetch_ls([(r.sym, int(r.entry_h) * 3600) for r in sig.itertuples()])
            sig["ls"] = [ls.get((r.sym, int(r.entry_h) * 3600), np.nan) for r in sig.itertuples()]
            cache[key] = sig
        sig = cache[key]
        return sig, ((sig.ls > 1.5) & (sig.majors <= 0.07) & (sig.majors >= -0.03)).to_numpy().copy()

    def delayed(lab, da, db):
        delay = int(lab.split("+")[1].rstrip("h"))
        sig, mask = check_hour("00:00", da, db)
        sig = sig.copy()
        sig["entry_h"] = sig.entry_h + delay
        return sig, mask

    out = ["## Best entry time (exits as in STRATEGY.md, 10% margin per trade)\n"]
    out += table("A. UTC hour of the daily check (entry at that hour)", check_hour,
                 [f"{h:02d}:00" for h in range(0, 24, 2)])
    out.append("")
    out += table("B. Check at 00:00 UTC, enter d hours later", delayed,
                 ["00:00 +0h", "00:00 +1h", "00:00 +2h", "00:00 +3h", "00:00 +4h", "00:00 +6h", "00:00 +8h", "00:00 +12h"])
    (HERE / "results" / "entry_time.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

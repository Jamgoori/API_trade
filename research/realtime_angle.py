"""Real-time entry at +30% (instead of +20%), split by how steep the run-up was.

Steepness = rise over the 4 hours before the crossing (rise_4h) or within the crossing hour (rise_1h).
Same exits as combination A: take-profit −15%, stop +15%, 3 days, drop a coin after a stop, 1x.
Scored in 2024-01..2025-06 and 2025-07..2026-10 separately, so a bucket that only works in one half shows up.
"""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from farm_filter import fetch_ls
from realtime_entry import intraday_signals
from short_study import H_DAY, load, pct, volume_ranks

HERE = Path(__file__).resolve().parent
PERIODS = {"2024-01..2025-06": ("2024-01-01", "2025-06-30"), "2025-07..2026-10": ("2025-07-01", "2026-10-31")}
FEATURES = {
    "rise_4h": ([-1, 0.10, 0.20, 0.30, 100], ["4h rise < 10% (slow)", "4h rise 10–20%", "4h rise 20–30%", "4h rise ≥ 30% (vertical)"]),
    "rise_1h": ([-1, 0.05, 0.10, 0.20, 100], ["1h candle < 5%", "1h candle 5–10%", "1h candle 10–20%", "1h candle ≥ 20%"]),
}


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    out = ["## Real-time entry at +30%, by steepness of the run-up (TP −15%, stop +15%, 3d, drop after stop, 1x)\n",
           "Per cell: trades, average return per trade, $10k at 15% margin per trade (max drawdown).\n"]
    for trig in (0.30,):
        for mode in ("instant", "hourly"):
            data = {}
            for pname, (a, b) in PERIODS.items():
                da = int(pd.Timestamp(a).timestamp() // 86400)
                db = min(int(pd.Timestamp(b).timestamp() // 86400), last)
                sig = intraday_signals(syms, ranks, da, db, mode, trig).sort_values("entry_h").reset_index(drop=True)
                ls = fetch_ls([(r.sym, int(r.entry_h) * 3600) for r in sig.itertuples()])
                sig["ls"] = [ls.get((r.sym, int(r.entry_h) * 3600), np.nan) for r in sig.itertuples()]
                data[pname] = (sig, g.Paths(syms, sig, g.MAX_H), da, db)
            for ratio in (None, 1.5):
                out.append(f"### +{trig:.0%} {mode}, ratio filter {'> 1.5' if ratio else 'none'}\n")
                out.append("| bucket | " + " | ".join(PERIODS) + " |")
                out.append("|---|" + "---|" * len(PERIODS))
                buckets = [("all", None, None)] + [(lab, f, (lo, hi)) for f, (edges, labs) in FEATURES.items()
                                                   for lab, lo, hi in zip(labs, edges[:-1], edges[1:])]
                for lab, f, rng in buckets:
                    cells = []
                    for pname, (sig, P, da, db) in data.items():
                        m = np.ones(len(sig), bool) if ratio is None else (sig.ls > ratio).to_numpy().copy()
                        if f:
                            m &= ((sig[f] >= rng[0]) & (sig[f] < rng[1])).to_numpy()
                        r = g.run_combo(sig, P, m, 0.15, 0.15, 3, True, da, db, margin_frac=0.15, return_curve=True)
                        cells.append("—" if r is None else
                                     f"{r['trades']}, {pct(r['mean'])}, ${r['curve'][-1] * 10_000:,.0f} ({pct(r['maxdd'], 0)})")
                    out.append(f"| {lab} | " + " | ".join(cells) + " |")
                    print(out[-1], flush=True)
                out.append("")
    (HERE / "results" / "realtime_angle.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

"""Red-day pump longs (BTC/ETH/SOL average −3% or worse, coin +20%): exits that win more often."""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from long_side import long_outcome
from short_study import H_DAY, load, pct, volume_ranks
from stop_and_hold import load_signals

HERE = Path(__file__).resolve().parent


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    da = int(pd.Timestamp("2024-01-01").timestamp() // 86400)
    sig = load_signals(syms, ranks, da, last, min_rank=0)
    sig = sig[sig.majors < -0.03].reset_index(drop=True)
    split = pd.Timestamp("2025-07-01").timestamp() // 86400
    early = (sig.day < split).to_numpy()
    out = ["## Red-day pump longs: exit grid (per signal)\n"]
    for slip, ss in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP = 0.0
        sig["entry_px"] = [syms[r.sym].open[(r.day + 1) * H_DAY - syms[r.sym].t0] * (1 + slip) for r in sig.itertuples()]
        P = g.Paths(syms, sig, 7 * H_DAY + 1)
        rows = []
        for tp in (0.03, 0.05, 0.08, 0.10, 0.15, 0.20, 0.30):
            for stop in (0.10, 0.15, 0.20, 0.30):
                for hold in (24, 48, 72, 168):
                    r = long_outcome(P, tp, stop, hold, slip, ss)
                    ok = ~np.isnan(r)
                    a, b = r[ok & early], r[ok & ~early]
                    rows.append((tp, stop, hold, np.mean(np.r_[a, b] > 0), np.mean(a), np.mean(b), np.mean(a > 0), np.mean(b > 0)))
        df = pd.DataFrame(rows, columns=["tp", "stop", "hold", "win", "early", "late", "win_e", "win_l"])
        good = df[(df.win >= 0.6) & (df.early > 0) & (df.late > 0)].sort_values(["win", "late"], ascending=False)
        out += [f"### Slippage {slip:.1%}: win rate 60%+ and positive in both halves\n",
                "| take-profit | stop | hold | win (early / late) | per trade early | per trade late |", "|---|---|---|---|---|---|"]
        for x in good.itertuples():
            out.append(f"| +{x.tp:.0%} | −{x.stop:.0%} | {x.hold // 24}d | {x.win:.0%} ({x.win_e:.0%} / {x.win_l:.0%}) | {pct(x.early)} | {pct(x.late)} |")
        out.append("")
        print("\n".join(out[-len(good) - 4:]), flush=True)
    (HERE / "results" / "long_winrate.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

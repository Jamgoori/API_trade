"""How often to check for signals: every 24h (the current rule) versus every 12h / 6h / 1h.

At each check hour H (UTC, a multiple of the interval) a coin qualifies when its close at H is >= +20% above
its close 24 hours earlier (for the 24h interval this is exactly the daily-close rule). Entry at the open of
hour H. The other filters use the same 24-hour window: BTC/ETH/SOL average move over the last 24h within
−3%..+7%, long/short account ratio > 1.5 at H, volume rank outside the top 100. One signal per coin per 24h.
Exits as in STRATEGY.md: take-profit −15%, stop +15%, 48 hours, drop a coin after a stop, 1x.
"""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from farm_filter import fetch_ls
from majors_filter import MAJORS
from short_study import H_DAY, load, pct, volume_ranks

HERE = Path(__file__).resolve().parent
PERIODS = {"2024-01..2025-06": ("2024-01-01", "2025-06-30"), "2025-07..2026-10": ("2025-07-01", "2026-10-31"),
           "2024-01..2026-10": ("2024-01-01", "2026-10-31")}


def rolling_signals(syms, ranks, h_a: int, h_b: int, step: int, trig: float = 0.20) -> pd.DataFrame:
    rows = []
    for name, s in syms.items():
        last_sig = -10 ** 9
        start = max(h_a, s.t0 + 25)
        start += (-start) % step
        for H in range(start, min(h_b, s.t0 + s.n), step):
            k = H - s.t0  # entry bar; the check uses the close of the bar before it
            if s.close[k - 1] < (1 + trig) * s.close[k - 25] or H - last_sig < 24:
                continue
            day = (H - 1) // H_DAY
            if day not in ranks.index or not ranks.at[day, name] > 100:
                continue
            last_sig = H
            rows.append({"sym": name, "entry_h": H, "day": H // H_DAY - 1})
    return pd.DataFrame(rows)


def majors_24h(syms, H: int) -> float:
    rets = []
    for m in MAJORS:
        s = syms[m]
        k = H - s.t0
        if 25 <= k <= s.n:
            rets.append(s.close[k - 1] / s.close[k - 25] - 1)
    return float(np.mean(rets)) if rets else np.nan


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    out = ["## Check interval (rolling 24h +20%, exits as in STRATEGY.md, 10% margin per trade)\n",
           "Per cell: trades, average return per trade, $10k → (max drawdown). Two slippage cases: "
           "0.1% / stop 0.5% (base) and 0.3% / stop 1.0%.\n",
           "| check every | slippage | " + " | ".join(PERIODS) + " |", "|---|---|" + "---|" * len(PERIODS)]
    for step in (24, 12, 6, 1):
        rows = {0.001: [], 0.003: []}
        for pname, (a, b) in PERIODS.items():
            da = int(pd.Timestamp(a).timestamp() // 86400)
            db = min(int(pd.Timestamp(b).timestamp() // 86400), last)
            sig = rolling_signals(syms, ranks, da * H_DAY, (db + 1) * H_DAY, step)
            sig = sig.sort_values("entry_h").reset_index(drop=True)
            sig["majors"] = [majors_24h(syms, int(h)) for h in sig.entry_h]
            ls = fetch_ls([(r.sym, int(r.entry_h) * 3600) for r in sig.itertuples()])
            sig["ls"] = [ls.get((r.sym, int(r.entry_h) * 3600), np.nan) for r in sig.itertuples()]
            mask = ((sig.ls > 1.5) & (sig.majors <= 0.07) & (sig.majors >= -0.03)).to_numpy().copy()
            for slip, stop_slip in ((0.001, 0.005), (0.003, 0.01)):
                g.SLIP, g.STOP_SLIP = slip, stop_slip
                P = g.Paths(syms, sig, g.MAX_H)
                r = g.run_combo(sig, P, mask, 0.15, 0.15, 2, True, da, db, margin_frac=0.10, return_curve=True)
                rows[slip].append(f"{r['trades']}, {pct(r['mean'])}, ${r['curve'][-1] * 10_000:,.0f} ({pct(r['maxdd'], 0)})")
            g.SLIP, g.STOP_SLIP = 0.001, 0.005
        for slip in rows:
            out.append(f"| {step}h | {slip:.1%} | " + " | ".join(rows[slip]) + " |")
            print(out[-1], flush=True)
    (HERE / "results" / "check_interval.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

"""Entry order type: market-ish entry at 00:00 versus a resting limit sell placed above the 00:00 price.

Limit variants: sell limit at open × (1 + offset), kept for `window` hours. If the price reaches it, the short is
filled exactly there (maker, no slippage); the take-profit cannot trigger in the fill hour, the stop can.
If it never fills: either skip the trade, or fall back to a market entry when the window ends.
Rule as in STRATEGY.md (incl. the wick filter), 10% margin, two slippage cases for market orders.
"""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from short_study import H_DAY, load, pct, volume_ranks
from stop_and_hold import load_signals
from wick_filter import day_shape

HERE = Path(__file__).resolve().parent
PERIODS = {"2024-01..2025-06": ("2024-01-01", "2025-06-30"), "2025-07..2026-10": ("2025-07-01", "2026-10-31"),
           "2024-01..2026-10": ("2024-01-01", "2026-10-31")}


def variant(syms, sig: pd.DataFrame, offset, window, fallback, slip):
    """Rows with entry_h / entry_px / partial for one order type; returns (frame, filled share)."""
    rows, filled = [], 0
    for r in sig.itertuples():
        s = syms[r.sym]
        H = (r.day + 1) * H_DAY
        i = H - s.t0
        base = r._asdict()
        if offset is None:  # market-ish entry at the 00:00 open
            rows.append(base | {"entry_h": H})
            continue
        lim = s.open[i] * (1 + offset)
        hit = np.flatnonzero(s.high[i:i + window] >= lim)
        if len(hit):
            b = int(hit[0])
            filled += 1
            # p0 = entry_px × (1 − SLIP) inside Paths; undo it so a limit fill gets exactly its price
            rows.append(base | {"entry_h": H + b, "entry_px": max(lim, s.open[i + b]) / (1 - slip), "partial": True})
        elif fallback and i + window < s.n:
            rows.append(base | {"entry_h": H + window})
    out = pd.DataFrame(rows).drop(columns=["Index"])
    if "entry_px" in out:
        out["partial"] = out["partial"].fillna(False).astype(bool)
    return out.sort_values("entry_h").reset_index(drop=True), filled / len(sig)


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    data = {}
    for pname, (a, b) in PERIODS.items():
        da = int(pd.Timestamp(a).timestamp() // 86400)
        db = min(int(pd.Timestamp(b).timestamp() // 86400), last)
        sig = load_signals(syms, ranks, da, db)
        sig["retrace"] = [day_shape(syms[r.sym], r.day)[1] for r in sig.itertuples()]
        sig = sig[(sig.ls > 1.5) & (sig.majors <= 0.07) & (sig.majors >= -0.03) & (sig.retrace <= 0.30)]
        data[pname] = (sig.reset_index(drop=True), da, db)
    variants = [("market at 00:00 (current)", None, 0, False)]
    for off in (0.003, 0.005, 0.01, 0.02):
        for win in (1, 4):
            variants.append((f"limit +{off:.1%}, {win}h, skip if unfilled", off, win, False))
        variants.append((f"limit +{off:.1%}, 1h, else market at 01:00", off, 1, True))
    out = ["## Entry order type (rule as in STRATEGY.md incl. wick filter, 10% margin, $10k)\n",
           "Per cell: trades, per trade, $10k → (max DD). Slippage applies to market orders only "
           "(entry, time exit, stop); limit fills get their exact price.\n",
           "| entry | filled | slippage | " + " | ".join(PERIODS) + " |", "|---|---|---|" + "---|" * len(PERIODS)]
    for label, off, win, fb in variants:
        for slip, stop_slip in ((0.001, 0.005), (0.003, 0.01)):
            g.SLIP, g.STOP_SLIP = slip, stop_slip
            cells, fill_share = [], None
            for pname, (sig, da, db) in data.items():
                v, fs = variant(syms, sig, off, win, fb, slip)
                fill_share = fs if fill_share is None else fill_share
                P = g.Paths(syms, v, g.MAX_H)
                r = g.run_combo(v, P, np.ones(len(v), bool), 0.15, 0.15, 2, True, da, db, margin_frac=0.10,
                                return_curve=True)
                cells.append(f"{r['trades']}, {pct(r['mean'])}, ${r['curve'][-1] * 10_000:,.0f} ({pct(r['maxdd'], 0)})")
            fill = "—" if off is None else f"{fill_share:.0%}"
            out.append(f"| {label} | {fill} | {slip:.1%} | " + " | ".join(cells) + " |")
            print(out[-1], flush=True)
    g.SLIP, g.STOP_SLIP = 0.001, 0.005
    (HERE / "results" / "limit_entry.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

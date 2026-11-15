"""Two questions about the final rule (STRATEGY.md):

1. Volatility-based stop: stop = entry × (1 + k × ATR%), ATR% = 14-day average true range / close on daily bars,
   clamped to 8%..50%, versus fixed stops of 10..30%. Picked on 2024-01..2025-06, checked on 2025-07..2026-10.
2. The 3-day time exit: what would the trades it closed have done if held until take-profit or stop (up to 30 days)?
"""

from pathlib import Path

import numpy as np
import pandas as pd

import tp_grid as g
from farm_filter import fetch_ls
from majors_filter import majors_day_return
from pump_day_short import signals
from short_study import H_DAY, load, pct, volume_ranks

HERE = Path(__file__).resolve().parent
PERIODS = {"2024-01..2025-06": ("2024-01-01", "2025-06-30"), "2025-07..2026-10": ("2025-07-01", "2026-10-31"),
           "2024-01..2026-10": ("2024-01-01", "2026-10-31")}


def atr_pct(s, day: int, n: int = 14, include_signal_day: bool = True) -> float:
    end = day if include_signal_day else day - 1
    trs = []
    for d in range(end - n + 1, end + 1):
        a, b = d * H_DAY - s.t0, (d + 1) * H_DAY - s.t0
        if a < 1 or b > s.n:
            continue
        hi, lo, pc, c = s.high[a:b].max(), s.low[a:b].min(), s.close[a - 1], s.close[b - 1]
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)) / c)
    return float(np.mean(trs)) if len(trs) >= 5 else np.nan


def load_signals(syms, ranks, da, db):
    sig = signals(syms, ranks, 0.20, da, db).sort_values("day").reset_index(drop=True)
    ls = fetch_ls([(r.sym, (r.day + 1) * 86400) for r in sig.itertuples()])
    sig["ls"] = [ls.get((r.sym, (r.day + 1) * 86400), np.nan) for r in sig.itertuples()]
    sig["majors"] = [majors_day_return(syms, d) for d in sig.day]
    sig["atr"] = [atr_pct(syms[r.sym], r.day) for r in sig.itertuples()]
    sig["atr_pre"] = [atr_pct(syms[r.sym], r.day, include_signal_day=False) for r in sig.itertuples()]
    return sig


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    data = {}
    for pname, (a, b) in PERIODS.items():
        da = int(pd.Timestamp(a).timestamp() // 86400)
        db = min(int(pd.Timestamp(b).timestamp() // 86400), last)
        sig = load_signals(syms, ranks, da, db)
        mask = ((sig.ls > 1.5) & (sig.majors <= 0.07) & (sig.majors >= -0.03)).to_numpy().copy()
        data[pname] = (sig, g.Paths(syms, sig, g.MAX_H), mask, da, db)

    out = ["## 1. Fixed versus volatility-based stop (TP −15%, 3 days, drop after stop, ratio > 1.5, majors filter)\n",
           "Per cell: trades, average return per trade, $10k at 15% margin per trade (max drawdown). "
           "ATR% = 14-day average true range / close.\n",
           "| stop | median stop width | " + " | ".join(PERIODS) + " |", "|---|---|" + "---|" * len(PERIODS)]
    stops = [(f"fixed +{x:.0%}", lambda sig, x=x: np.full(len(sig), x)) for x in (0.10, 0.12, 0.15, 0.20, 0.25, 0.30)]
    for col, name in (("atr", "ATR incl. pump day"), ("atr_pre", "ATR before pump")):
        for k in (1.0, 1.5, 2.0, 2.5, 3.0):
            stops.append((f"{k:g} × {name}", lambda sig, k=k, col=col: np.clip(k * sig[col].fillna(0.15 / k).to_numpy(), 0.08, 0.50)))
    for label, fn in stops:
        cells, width = [], None
        for pname, (sig, P, mask, da, db) in data.items():
            st = fn(sig)
            width = np.median(st[mask]) if width is None else width
            r = g.run_combo(sig, P, mask, 0.15, st, 3, True, da, db, margin_frac=0.15, return_curve=True)
            cells.append(f"{r['trades']}, {pct(r['mean'])}, ${r['curve'][-1] * 10_000:,.0f} ({pct(r['maxdd'], 0)})")
        out.append(f"| {label} | +{width:.0%} | " + " | ".join(cells) + " |")
        print(out[-1], flush=True)

    # 2. the 3-day exit
    sig, P, mask, da, db = data["2024-01..2026-10"]
    r3, j3, w3, _ = P.outcome(0.15, 0.15, 3)
    rinf, jinf, winf, _ = P.outcome(0.15, 0.15, 30)
    taken, state = [], {}
    for rr in np.flatnonzero(mask):
        if np.isnan(r3[rr]):
            continue
        sym = sig.sym.iat[rr]
        ff, banned = state.get(sym, (-1, False))
        if banned or P.entry_h[rr] <= ff:
            continue
        taken.append(rr)
        state[sym] = (P.entry_h[rr] + j3[rr], w3[rr] >= 2)
    taken = np.array(taken)
    timed = taken[w3[taken] == 0]
    later = winf[timed]
    out.append("\n## 2. Trades closed by the 3-day limit, and what holding on (up to 30 days) would have done\n")
    out.append(f"{len(timed)} of {len(taken)} trades ({len(timed) / len(taken):.0%}) were closed at 3 days, "
               f"averaging {pct(np.mean(r3[timed]))} at the close.\n")
    out.append("| if held on | share | average result if held |")
    out.append("|---|---|---|")
    for code, name in ((1, "take-profit −15% hit later"), (2, "stop +15% hit later"), (0, "neither within 30 days")):
        sel = later == code
        out.append(f"| {name} | {sel.mean():.0%} | {pct(np.nanmean(rinf[timed][sel])) if sel.any() else '—'} |")
    out.append(f"\nAverage per closed trade: {pct(np.mean(r3[timed]))} with the 3-day cut versus "
               f"{pct(np.nanmean(rinf[timed]))} if held up to 30 days.")
    (HERE / "results" / "stop_and_hold.md").write_text("\n".join(out) + "\n")
    print("\n".join(out[-9:]))


if __name__ == "__main__":
    main()

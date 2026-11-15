"""Chicken farm with a crowd-positioning filter: does Binance's long/short account ratio keep the blow-ups out?

Rules tested on the 101+ volume tier, 1x isolated, 30-day cohorts started on four staggered offsets
(0/7/14/21 days) so one lucky start date cannot carry the result:

- entry filter: skip coins whose long/short account ratio at entry is below a threshold
  (most accounts already short = squeeze fuel), or coins listed less than 30 days ago;
- exit rule: close a short when a pump signal fires and the ratio at that moment is below a threshold.

The ratio at entry is the last 5-minute snapshot of the previous UTC day, so nothing after the entry is used.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from binance_data import metrics
from short_study import H_DAY, Hedge, equity_stats, load, pct, pump_signal, sim_short, volume_ranks


def fetch_ls(queries: list[tuple[str, int]], workers: int = 48) -> dict[tuple[str, int], float]:
    """Long/short account ratio at the latest snapshot <= ts (within 30 min) for each (symbol, unix ts)."""
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for sym, ts in set(queries):
        day = pd.Timestamp(ts - 1, unit="s").strftime("%Y-%m-%d")
        groups[(sym, day)].append(ts)

    def one(item):
        (sym, day), tss = item
        try:
            m = metrics(sym, day)
        except Exception:
            m = None
        if m is None or m.empty:
            return [((sym, t), np.nan) for t in tss]
        mts, ls = m["ts"].to_numpy(), m["ls_accounts"].to_numpy(float)
        out = []
        for t in tss:
            k = np.searchsorted(mts, t, side="right") - 1
            out.append(((sym, t), ls[k] if k >= 0 and t - mts[k] <= 1800 else np.nan))
        return out

    res: dict[tuple[str, int], float] = {}
    with ThreadPoolExecutor(workers) as ex:
        for part in ex.map(one, groups.items()):
            res.update(part)
    return res


def run(start: str, end: str, out: Path) -> str:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    hedge = Hedge(syms["BTCUSDT"])
    day_a = int(pd.Timestamp(start).timestamp() // 86400)
    day_b = min(int(pd.Timestamp(end).timestamp() // 86400), max(s.t0 + s.n for s in syms.values()) // H_DAY) - 30

    # cohorts: four staggered 30-day schedules
    schedules = {off: list(range(day_a + off, day_b, 30)) for off in (0, 7, 14, 21)}
    entries = []  # (offset, day, sym)
    for off, days in schedules.items():
        for day in days:
            r = ranks.loc[day]
            for name in r[r > 100].index:
                s = syms[name]
                if 0 <= day * H_DAY - s.t0 < s.n:
                    entries.append((off, day, name))

    # pump signals during the window, for coins that were ever held
    held = {e[2] for e in entries}
    sig = {}
    for name in held:
        s = syms[name]
        m = pump_signal(s)[0]
        lo, hi = day_a * H_DAY - s.t0, (day_b + 30) * H_DAY - s.t0
        keep = np.zeros(s.n, bool)
        keep[max(lo, 0):max(hi, 0)] = True
        sig[name] = m & keep

    queries = [(name, day * 86400) for _, day, name in entries]
    queries += [(name, (syms[name].t0 + int(h) + 1) * 3600) for name in held for h in np.flatnonzero(sig[name])]
    print(f"{len(entries)} cohort entries, {sum(int(v.sum()) for v in sig.values())} signal bars, "
          f"{len(set(queries))} ratio lookups", flush=True)
    ls = fetch_ls(queries)

    ls_entry = {(day, name): ls.get((name, day * 86400), np.nan) for _, day, name in entries}
    ls_sig = {}
    for name in held:
        s = syms[name]
        arr = np.full(s.n, np.nan)
        for h in np.flatnonzero(sig[name]):
            arr[h] = ls.get((name, (s.t0 + int(h) + 1) * 3600), np.nan)
        ls_sig[name] = arr
    cov_entry = np.mean([not np.isnan(v) for v in ls_entry.values()])
    sig_vals = np.concatenate([ls_sig[n][sig[n]] for n in held if sig[n].any()])
    cov_sig = np.mean(~np.isnan(sig_vals)) if len(sig_vals) else 0.0

    def exit_mask(thr):
        if thr is None:
            return None
        if thr == "any":
            return sig
        return {n: sig[n] & (ls_sig[n] < thr) for n in held}

    rules = [
        ("no filter", None, False, None, None),
        ("no filter, +30% stop", None, False, None, 0.3),
        ("skip coins listed < 30 days", None, True, None, None),
        ("entry: skip ratio < 0.7", 0.7, False, None, None),
        ("entry: skip ratio < 1.0", 1.0, False, None, None),
        ("exit on pump signal (any ratio)", None, False, "any", None),
        ("exit on pump signal when ratio < 0.7", None, False, 0.7, None),
        ("exit on pump signal when ratio < 1.0", None, False, 1.0, None),
        ("entry < 1.0 skipped + exit when ratio < 1.0", 1.0, False, 1.0, None),
        ("entry < 1.0 skipped + exit when ratio < 1.0 + skip new listings", 1.0, True, 1.0, None),
        ("entry < 1.0 skipped + exit when ratio < 1.0, +50% stop", 1.0, False, 1.0, 0.5),
        ("entry < 1.0 skipped + exit when ratio < 1.0, +30% stop", 1.0, False, 1.0, 0.3),
    ]

    lines = [f"### {start[:7]} .. {pd.Timestamp(day_b * 86400, unit='s'):%Y-%m} (cohort starts), volume rank 101+, 1x isolated, 30-day hold\n",
             f"Ratio found for {cov_entry:.0%} of entries and {cov_sig:.0%} of signal bars; "
             "entry filters drop coins with no ratio (mostly listings younger than a day or two).\n",
             "CAGR and max drawdown are averaged over the four staggered schedules; the worst schedule's drawdown is in brackets.\n",
             "| rule | coins/cohort | liquidated | closed early | CAGR | max DD (worst) | CAGR, BTC-hedged |",
             "|---|---|---|---|---|---|---|"]
    trades_out = []
    for label, entry_thr, skip_new, exit_thr, stop in rules:
        mask = exit_mask(exit_thr)
        cagr, dd, ddw, hcagr, n, liq, early = [], [], [], [], [], [], []
        for off, days in schedules.items():
            rows = []
            for day in days:
                r = ranks.loc[day]
                tr = []
                for name in r[r > 100].index:
                    s = syms[name]
                    i = day * H_DAY - s.t0
                    if not 0 <= i < s.n:
                        continue
                    if skip_new and i < 30 * H_DAY:
                        continue
                    if entry_thr is not None:
                        v = ls_entry.get((day, name), np.nan)
                        if np.isnan(v) or v < entry_thr:
                            continue
                    if mask is not None:
                        s.signal = mask[name]
                    res = sim_short(s, i, 30 * H_DAY, 1.0, stop, exit_on_signal=mask is not None)
                    if res is None:
                        continue
                    ret, why, j = res
                    hed = hedge.ret(day * H_DAY, s.t0 + j)
                    tr.append((ret, why, ret + hed))
                    trades_out.append((label, off, day, name, ret, why))
                if tr:
                    t = pd.DataFrame(tr, columns=["ret", "why", "hedged"])
                    rows.append({"ret": t.ret.mean(), "hedged": t.hedged.mean(), "n": len(t),
                                 "liq": (t.why == "liq").mean(), "early": t.why.isin(["stop", "signal"]).mean()})
            c = pd.DataFrame(rows)
            st, sh = equity_stats(c.ret, 365 / 30), equity_stats(c.hedged, 365 / 30)
            cagr.append(st["cagr"]); dd.append(st["maxdd"]); hcagr.append(sh["cagr"])
            n.append(c.n.mean()); liq.append(c.liq.mean()); early.append(c.early.mean())
        lines.append(f"| {label} | {np.mean(n):.0f} | {np.mean(liq):.1%} | {np.mean(early):.0%} | {pct(np.mean(cagr), 0)} "
                     f"| {pct(np.mean(dd), 0)} ({pct(min(dd), 0)}) | {pct(np.mean(hcagr), 0)} |")
        print(lines[-1], flush=True)
    pd.DataFrame(trades_out, columns=["rule", "offset", "day", "sym", "ret", "why"]).to_csv(
        out / f"farm_filter_trades_{start[:4]}_{end[:4]}.csv.gz", index=False)
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", type=Path, default=Path(__file__).resolve().parent / "results")
    a = ap.parse_args()
    a.save.mkdir(parents=True, exist_ok=True)
    parts = ["## 6. Chicken farm with a long/short-ratio filter\n"]
    for start, end in [("2025-01-01", "2026-11-01"), ("2022-01-01", "2025-01-01")]:
        parts.append(run(start, end, a.save))
        parts.append("")
    (a.save / "farm_filter.md").write_text("\n".join(parts) + "\n")


if __name__ == "__main__":
    main()

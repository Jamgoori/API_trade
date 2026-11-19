"""Do Upbit BTC-market (and USDT-market) listings pump the Binance perp like KRW listings do?

Same rule as the listing sleeve in STRATEGY.md: the coin's Binance USDT perp has traded 30+ days, short it one hour
after the listing hour, TP −15% / stop +15% / 48h. Events that fall within 3 days of a listing already in
results/listings.csv (KRW / Binance spot) are dropped, since the current sleeve already trades those.
"""

from pathlib import Path
import time

import numpy as np
import pandas as pd

import tp_grid as g
from combined import PERIODS, portfolio, sleeve_trades
from funding_filter import funding_at_entry, skip_rule
from limit_entry import variant
from listings import S, START, krw_first_candle, krw_listing_hour
from short_study import H_DAY, load, pct, volume_ranks
from stop_and_hold import load_signals
from wick_filter import day_shape

HERE = Path(__file__).resolve().parent
CACHE = HERE.parent / "data" / "listings"
UPBIT = "https://api.upbit.com"


def upbit_listings(prefix: str) -> pd.DataFrame:
    path = CACHE / f"listings_upbit_{prefix.lower()}.csv"
    if path.exists():
        return pd.read_csv(path, parse_dates=["listed"])
    markets = [m["market"] for m in S.get(f"{UPBIT}/v1/market/all", timeout=20).json()
               if m["market"].startswith(prefix + "-")]
    out = []
    for m in markets:
        first = krw_first_candle(UPBIT, m)
        if first is None or first < START:
            continue
        out.append({"venue": f"upbit_{prefix.lower()}", "base": m.split("-", 1)[1],
                    "listed": krw_listing_hour(UPBIT, m, first)})
        time.sleep(0.12)
    f = pd.DataFrame(out, columns=["venue", "base", "listed"])
    f.to_csv(path, index=False)
    return f


def main() -> None:
    syms = load()
    perp_start = {n: pd.Timestamp(s.t0 * 3600, unit="s", tz="UTC") for n, s in syms.items()}
    known = pd.read_csv(HERE / "results" / "listings.csv", parse_dates=["listed"])
    ev = []
    for prefix in ("BTC", "USDT"):
        f = upbit_listings(prefix)
        f["listed"] = pd.to_datetime(f["listed"], utc=True)
        print(f"upbit {prefix} markets listed since {START.date()}: {len(f)}", flush=True)
        for r in f.itertuples():
            for perp in (f"{r.base}USDT", f"1000{r.base}USDT", f"1000000{r.base}USDT"):
                if perp in perp_start:
                    age = (r.listed - perp_start[perp]).days
                    dup = ((known.perp == perp) & ((known.listed - r.listed).abs() <= pd.Timedelta(days=3))).any()
                    ev.append({"venue": r.venue, "perp": perp, "listed": r.listed, "age": age, "dup": dup})
                    break
    ev = pd.DataFrame(ev)
    print(ev.groupby(["venue", ev.age >= 30, "dup"]).size().to_string(), flush=True)
    ev = ev[(ev.age >= 30) & ~ev.dup]
    hours = (ev.listed - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(hours=1)
    new = pd.DataFrame({"sym": ev.perp.values, "entry_h": hours.astype(int).values + 1, "venue": ev.venue.values})
    new = new[[s in syms and syms[s].t0 <= h < syms[s].t0 + syms[s].n for s, h in zip(new.sym, new.entry_h)]]
    new["day"] = new.entry_h // H_DAY - 1

    out = ["## Upbit BTC / USDT market listings (perp 30+ days old, not within 3 days of a KRW / Binance spot listing)\n",
           "Short one hour after the listing hour, TP −15% / stop +15% / 48h.\n"]
    split = pd.Timestamp("2025-07-01").timestamp() // 3600
    for slip, ss in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP, g.STOP_SLIP = slip, ss
        P = g.Paths(syms, new.reset_index(drop=True), g.MAX_H)
        ret = P.outcome(0.15, 0.15, 2)[0]
        out += [f"### Per trade, slippage {slip:.1%}\n", "| venue | 2024-01..2025-06 | 2025-07..2026-10 |", "|---|---|---|"]
        for v in ("upbit_btc", "upbit_usdt"):
            cells = []
            for early in (True, False):
                sel = ((new.venue == v) & ((new.entry_h < split) if early else (new.entry_h >= split))).to_numpy() & ~np.isnan(ret)
                cells.append(f"{sel.sum()}, {pct(np.mean(ret[sel]))}, win {np.mean(ret[sel] > 0):.0%}" if sel.any() else "—")
            out.append(f"| {v} | " + " | ".join(cells) + " |")
        out.append("")
    print("\n".join(out), flush=True)

    # portfolio: main + listing, with and without the Upbit BTC events
    da = int(pd.Timestamp("2024-01-01").timestamp() // 86400)
    d0, ranks, _ = volume_ranks(syms)
    last = max(s.t0 + s.n for s in syms.values()) // H_DAY - 1
    sig = load_signals(syms, ranks, da, last)
    sig["retrace"] = [day_shape(syms[r.sym], r.day)[1] for r in sig.itertuples()]
    sig["skipf"] = [skip_rule(*funding_at_entry(syms[r.sym], (r.day + 1) * H_DAY)) for r in sig.itertuples()]
    sig = sig[(sig.ls > 1.5) & (sig.majors <= 0.07) & (sig.majors >= -0.03) & (sig.retrace <= 0.30) & ~sig.skipf].reset_index(drop=True)
    lst = known.copy()
    lh = (lst.listed - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(hours=1)
    lst = pd.DataFrame({"sym": lst.perp, "entry_h": lh.astype(int) + 1})
    lst = lst[[s in syms and syms[s].t0 <= h < syms[s].t0 + syms[s].n for s, h in zip(lst.sym, lst.entry_h)]]
    lst["day"] = lst.entry_h // H_DAY - 1
    out += ["### Portfolio, 12% per trade, $10k (final, max drawdown)\n",
            "| sleeves | " + " | ".join(PERIODS) + " | slippage |", "|---|" + "---|" * len(PERIODS) + "---|"]
    for slip, ss in ((0.001, 0.005), (0.003, 0.01)):
        g.SLIP, g.STOP_SLIP = slip, ss
        m, _ = variant(syms, sig, 0.005, 1, True, slip)
        mt = sleeve_trades("main", m, syms, 0.15, 0.15, 2, True)
        lt = sleeve_trades("listing", lst, syms, 0.15, 0.15, 2, False)
        bt = [("listing",) + t[1:] for t in sleeve_trades("x", new[new.venue == "upbit_btc"], syms, 0.15, 0.15, 2, False)]
        ut = [("listing",) + t[1:] for t in sleeve_trades("x", new, syms, 0.15, 0.15, 2, False)]
        for label, tr in (("main + listing (now)", mt + lt), ("+ Upbit BTC market", mt + lt + bt),
                          ("+ Upbit BTC and USDT markets", mt + lt + ut)):
            cells = []
            for pname, (a, b) in PERIODS.items():
                pa = int(pd.Timestamp(a).timestamp() // 86400)
                pb = min(int(pd.Timestamp(b).timestamp() // 86400), last)
                fin, dd, _ = portfolio(tr, pa, pb, 0.12, ("main", "listing"))
                cells.append(f"${fin:,.0f} ({pct(dd, 0)})")
            out.append(f"| {label} | " + " | ".join(cells) + f" | {slip:.1%} |")
            print(out[-1], flush=True)
    g.SLIP, g.STOP_SLIP = 0.001, 0.005
    (HERE / "results" / "upbit_btc_listing.md").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

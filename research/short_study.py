"""Does shorting Binance small-cap perps pay, and what happens after a pump?

Reads the per-symbol panels written by binance_data.py and prints markdown tables:

1. cohorts  - the "chicken farm": every 30 days, short every small-cap coin with equal margin, hold 30 days.
2. events   - pump events (4h return and volume spike): what the price does next.
3. fade     - shorting after a pump, entered at different delays.

All prices are Binance 1h bars. Trades enter at a bar open and pay taker fees, slippage and funding.
Isolated margin: a liquidation loses the whole margin of that position and nothing else.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent / "data" / "binance"
H_DAY = 24

FEE = 0.0005        # Binance USD-M taker
SLIP = 0.001        # per side, normal fills
STOP_SLIP = 0.005   # extra on stop fills: they fire into a rising book
MMR = 0.025         # maintenance margin rate assumed for small caps


@dataclass
class Sym:
    name: str
    t0: int                 # absolute hour of first bar
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    qvol: np.ndarray
    fund: np.ndarray        # funding rate charged at the start of that bar (0 if none)
    delisted: bool
    signal: np.ndarray | None = None  # pump signal known at the close of that bar

    @property
    def n(self) -> int:
        return len(self.close)


def load(min_bars: int = 24 * 14) -> dict[str, Sym]:
    raw = {}
    for p in sorted((ROOT / "panel").glob("*.npz")):
        z = np.load(p)
        if len(z["t"]) >= min_bars:
            raw[p.stem] = z
    last = max(int(z["t"][-1]) for z in raw.values())
    out = {}
    for name, z in raw.items():
        t = z["t"]
        t0 = int(t[0])
        n = int(t[-1]) - t0 + 1
        idx = t - t0
        close = np.full(n, np.nan)
        close[idx] = z["close"]
        close = pd.Series(close).ffill().to_numpy()
        def fill(a):
            x = close.copy()
            x[idx] = a
            return x
        qvol = np.zeros(n)
        qvol[idx] = z["qvol"]
        fund = np.zeros(n)
        ft = z["fund_t"].astype(np.int64) - t0
        ok = (ft >= 0) & (ft < n)
        np.add.at(fund, ft[ok], z["fund_rate"][ok])
        out[name] = Sym(name, t0, fill(z["open"]), fill(z["high"]), fill(z["low"]), close, qvol, fund,
                        delisted=int(t[-1]) < last - 48)
    return out


def pump_signal(s: Sym, r4_min: float = 0.20, vol_mult: float = 5) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """4h return >= r4_min and this hour's volume >= vol_mult x its trailing 7-day hourly average."""
    c = s.close
    r4 = np.full(s.n, np.nan)
    r4[4:] = c[4:] / c[:-4] - 1
    trail = pd.Series(s.qvol).rolling(168, min_periods=24).mean().shift(1).to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        vr = s.qvol / trail
    return (r4 >= r4_min) & (vr >= vol_mult), r4, vr


def mark_signals(syms: dict[str, Sym], **kw) -> None:
    for s in syms.values():
        s.signal = pump_signal(s, **kw)[0]


def volume_ranks(syms: dict[str, Sym]) -> tuple[int, pd.DataFrame, pd.DataFrame]:
    """Rank of each coin by trailing 30-day average daily volume, known at the start of each day."""
    d0 = min(s.t0 for s in syms.values()) // H_DAY
    d1 = max(s.t0 + s.n for s in syms.values()) // H_DAY + 1
    vol = pd.DataFrame(np.nan, index=range(d0, d1), columns=list(syms))
    for name, s in syms.items():
        days = (s.t0 + np.arange(s.n)) // H_DAY
        v = pd.Series(s.qvol).groupby(days).sum()
        vol.loc[v.index, name] = v.to_numpy()
    trailing = vol.rolling(30, min_periods=1).mean().shift(1)  # only past days
    ranks = trailing.rank(axis=1, ascending=False)
    return d0, ranks, trailing


# --------------------------------------------------------------------------- trade simulation

def sim_short(s: Sym, i: int, hours: int, lev: float, stop: float | None,
              exit_on_signal: bool = False) -> tuple[float, str, int] | None:
    """Short at open of bar i, hold `hours` bars. Returns (return on margin, exit reason, exit bar) or None.

    stop: exit when price rises this fraction above entry (None = hold until liquidation or the end).
    exit_on_signal: also close at the close of the first bar with a pump signal (see mark_signals).
    """
    end = i + hours
    reason_end = "end"
    if end > s.n:
        if not s.delisted:
            return None  # future not in the data yet
        end, reason_end = s.n, "delist"
    if end - i < 2:
        return None
    p0 = s.open[i] * (1 - SLIP)
    o = s.open[i:end] / p0
    h = s.high[i:end] / p0
    f = s.fund[i:end] * o
    f[0] = 0.0
    fc = np.cumsum(f)
    liqx = (1 / lev + 1 + fc - FEE) / (1 + MMR)
    trig = h >= liqx
    if stop is not None:
        trig |= h >= 1 + stop
    js = None
    if exit_on_signal:
        sig = s.signal[i:end].copy()
        sig[0] = False
        if sig.any():
            js = int(np.argmax(sig))
    if js is not None and not trig[: js + 1].any():
        x = s.close[i + js] / p0 * (1 + STOP_SLIP)
        if x >= liqx[js]:
            return -1.0, "liq", i + js
        return lev * ((1 - x) + fc[js] - FEE - FEE * x), "signal", i + js
    if trig.any():
        j = int(np.argmax(trig))
        if o[j] >= liqx[j]:
            return -1.0, "liq", i + j
        if stop is not None and h[j] >= 1 + stop:
            x = max(1 + stop, o[j]) * (1 + STOP_SLIP)
            if x >= liqx[j]:
                return -1.0, "liq", i + j
            return lev * ((1 - x) + fc[j] - FEE - FEE * x), "stop", i + j
        return -1.0, "liq", i + j
    x = s.close[end - 1] / p0 * (1 + SLIP)
    return lev * ((1 - x) + fc[-1] - FEE - FEE * x), reason_end, end - 1


class Hedge:
    """Long BTC over the same hours as a short: price return minus funding paid minus fees."""

    def __init__(self, btc: Sym):
        self.b = btc
        self.fcum = np.cumsum(btc.fund)

    def ret(self, h_in: int, h_out: int) -> float:
        b = self.b
        i, j = h_in - b.t0, h_out - b.t0
        if i < 0 or j >= b.n:
            return np.nan
        px = b.close[j] / b.open[i] - 1
        return px - (self.fcum[j] - self.fcum[i]) - 2 * (FEE + SLIP)


# --------------------------------------------------------------------------- 1. cohorts

def cohorts(syms, d0, ranks, *, tier: tuple[int, int], hold_days: int, lev: float, stop, hedge: Hedge,
            start_day: int, end_day: int, exit_on_signal: bool = False) -> pd.DataFrame:
    rows = []
    for day in range(start_day, end_day, hold_days):
        if day not in ranks.index:
            continue
        r = ranks.loc[day]
        names = r[(r > tier[0]) & (r <= tier[1])].index
        trades = []
        for name in names:
            s = syms[name]
            i = day * H_DAY - s.t0
            if i < 0 or i >= s.n:
                continue
            res = sim_short(s, i, hold_days * H_DAY, lev, stop, exit_on_signal)
            if res is None:
                continue
            ret, why, j = res
            hed = hedge.ret(day * H_DAY, s.t0 + j)
            trades.append((ret, why, ret + lev * hed if why != "liq" else -1.0 + lev * hed))
        if not trades:
            continue
        t = pd.DataFrame(trades, columns=["ret", "why", "hedged"])
        rows.append({"day": day, "n": len(t), "ret": t["ret"].mean(), "hedged": t["hedged"].mean(),
                     "liq": (t["why"] == "liq").mean(), "stop": (t["why"].isin(["stop", "signal"])).mean(),
                     "win": (t["ret"] > 0).mean()})
    return pd.DataFrame(rows)


def equity_stats(r: pd.Series, periods_per_year: float) -> dict:
    eq = (1 + r.clip(lower=-1)).cumprod()
    years = len(r) / periods_per_year
    peak = eq.cummax()
    return {"final": eq.iloc[-1], "cagr": eq.iloc[-1] ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1.0,
            "maxdd": (eq / peak - 1).min(), "worst": r.min(), "best": r.max(), "pos": (r > 0).mean()}


# --------------------------------------------------------------------------- 2. events

def find_events(syms, d0, ranks, *, r4_min: float, vol_mult: float, cooldown: int = 72) -> pd.DataFrame:
    rows = []
    for name, s in syms.items():
        if s.n < 24 * 3:
            continue
        sig, r4, vr = pump_signal(s, r4_min, vol_mult)
        cand = np.flatnonzero(sig)
        last_fund_idx = np.flatnonzero(s.fund != 0)
        nxt = -1
        for h in cand:
            if h < nxt:
                continue
            nxt = h + cooldown
            day = (s.t0 + h) // H_DAY
            rank = ranks.at[day, name] if day in ranks.index else np.nan
            k = np.searchsorted(last_fund_idx, h, side="right") - 1
            fund_now = s.fund[last_fund_idx[k]] if k >= 0 else np.nan
            rows.append({"sym": name, "h": int(h), "abs_h": s.t0 + int(h), "day": day, "rank": rank,
                         "age_d": h / 24, "r4": r4[h], "vr": vr[h], "fund": fund_now})
    return pd.DataFrame(rows)


def forward(syms, ev: pd.DataFrame) -> pd.DataFrame:
    out = []
    for e in ev.itertuples():
        s = syms[e.sym]
        h = e.h
        if h + 168 >= s.n:
            out.append(None)
            continue
        p = s.close[h]
        nx = slice(h + 1, h + 169)
        hi = s.high[nx] / p - 1
        lo = s.low[nx] / p - 1
        up20 = np.argmax(hi >= 0.2) if (hi >= 0.2).any() else 999
        dn20 = np.argmax(lo <= -0.2) if (lo <= -0.2).any() else 999
        out.append({
            "r1h": s.close[h + 1] / p - 1, "r4h": s.close[h + 4] / p - 1, "r24h": s.close[h + 24] / p - 1,
            "r3d": s.close[h + 72] / p - 1, "r7d": s.close[h + 168] / p - 1,
            "up24": hi[:24].max(), "up7d": hi.max(), "dn7d": lo.min(),
            "up20_first": up20 < dn20, "dn20_first": dn20 < up20,
            "fund7d": s.fund[nx].sum(),
        })
    keep = [i for i, o in enumerate(out) if o is not None]
    return pd.concat([ev.iloc[keep].reset_index(drop=True), pd.DataFrame([out[i] for i in keep])], axis=1)


# --------------------------------------------------------------------------- reporting helpers

def pct(x, d=1):
    return "—" if pd.isna(x) else f"{x * 100:+.{d}f}%"


def fwd_table(df: pd.DataFrame, by: str | None = None) -> str:
    groups = [("all", df)] if by is None else list(df.groupby(by, observed=True))
    lines = ["| group | events | 1h mean / median | 24h mean / median | 7d mean / median | another +20% first | −20% first | 7d max rise (median) |",
             "|---|---|---|---|---|---|---|---|"]
    for g, x in groups:
        lines.append(
            f"| {g} | {len(x)} | {pct(x.r1h.mean())} / {pct(x.r1h.median())} | {pct(x.r24h.mean())} / {pct(x.r24h.median())} "
            f"| {pct(x.r7d.mean())} / {pct(x.r7d.median())} | {x.up20_first.mean():.0%} | {x.dn20_first.mean():.0%} "
            f"| {pct(x.up7d.median())} |")
    return "\n".join(lines)


def add_oi(fw: pd.DataFrame, workers: int = 48) -> pd.DataFrame:
    """Attach open interest change and long/short account ratio at each signal (Binance 5-min metrics)."""
    from concurrent.futures import ThreadPoolExecutor

    from binance_data import metrics

    def one(e):
        t_sig = (e.abs_h + 1) * 3600  # signal bar close
        frames = []
        for back in (1, 0):
            day = pd.Timestamp((e.day - back) * 86400, unit="s").strftime("%Y-%m-%d")
            m = metrics(e.sym, day)
            if m is not None:
                frames.append(m)
        if not frames:
            return None
        m = pd.concat(frames).drop_duplicates("ts").set_index("ts").sort_index()
        m = m[m["oi"] > 0]
        def at(ts):
            x = m.loc[:ts]
            return x.iloc[-1] if len(x) and ts - x.index[-1] <= 1800 else None
        now, h4, h24 = at(t_sig), at(t_sig - 4 * 3600), at(t_sig - 24 * 3600)
        if now is None:
            return None
        return {"oi_4h": now["oi"] / h4["oi"] - 1 if h4 is not None else np.nan,
                "oi_24h": now["oi"] / h24["oi"] - 1 if h24 is not None else np.nan,
                "ls_accounts": now["ls_accounts"], "taker_ls": now["taker_ls_vol"]}

    with ThreadPoolExecutor(workers) as ex:
        res = list(ex.map(lambda e: _quiet(one, e), fw.itertuples()))
    extra = pd.DataFrame([r or {} for r in res], index=fw.index)
    return fw.join(extra)


def _quiet(fn, x):
    try:
        return fn(x)
    except Exception:
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--part", choices=["cohorts", "events", "fade", "oi", "all"], default="all")
    ap.add_argument("--save", type=Path, default=Path(__file__).resolve().parent / "results")
    a = ap.parse_args()
    a.save.mkdir(parents=True, exist_ok=True)

    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    hedge = Hedge(syms["BTCUSDT"])
    first_day = d0 + 31
    last_day = max(s.t0 + s.n for s in syms.values()) // H_DAY
    print(f"{len(syms)} coins, {sum(s.delisted for s in syms.values())} delisted, "
          f"days {first_day}..{last_day}\n", flush=True)

    if a.part in ("cohorts", "all"):
        print("## 1. Chicken farm: short every coin in a volume tier, equal margin, 30-day cohorts\n")
        print("| tier (volume rank) | leverage | stop | cohorts | coins/cohort | CAGR | max DD | worst month | months up | liquidated | stopped | CAGR, BTC-hedged | max DD, hedged |")
        print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        allc = []
        mark_signals(syms)
        variants = [(None, False), (0.5, False), (0.3, False), (None, True), (0.5, True)]
        for tier in [(30, 100), (100, 10_000)]:
            for lev in (1.0, 1.5):
                for stop, sig in variants:
                    c = cohorts(syms, d0, ranks, tier=tier, hold_days=30, lev=lev, stop=stop, hedge=hedge,
                                start_day=first_day, end_day=last_day - 30, exit_on_signal=sig)
                    c["tier"], c["lev"], c["stop"], c["signal_exit"] = str(tier), lev, str(stop), sig
                    allc.append(c)
                    st, sh = equity_stats(c["ret"], 365 / 30), equity_stats(c["hedged"], 365 / 30)
                    tname = "31–100" if tier[0] == 30 else "101+"
                    sname = ("none" if stop is None else f"+{stop:.0%}") + (" + pump signal" if sig else "")
                    print(f"| {tname} | {lev}x | {sname} | {len(c)} | {c['n'].mean():.0f} "
                          f"| {pct(st['cagr'], 0)} | {pct(st['maxdd'], 0)} | {pct(st['worst'], 0)} | {st['pos']:.0%} "
                          f"| {c['liq'].mean():.0%} | {c['stop'].mean():.0%} | {pct(sh['cagr'], 0)} | {pct(sh['maxdd'], 0)} |",
                          flush=True)
        pd.concat(allc).to_csv(a.save / "cohorts.csv", index=False)

        # year by year for the plain 1x, no-stop, 101+ farm
        c = cohorts(syms, d0, ranks, tier=(100, 10_000), hold_days=30, lev=1.0, stop=None, hedge=hedge,
                    start_day=first_day, end_day=last_day - 30)
        c["year"] = pd.to_datetime(c["day"] * 86400, unit="s").dt.year
        print("\n### 101+ tier, 1x, no stop, by year\n")
        print("| year | cohorts | coins/cohort | mean month | compounded | BTC-hedged compounded | liquidated |")
        print("|---|---|---|---|---|---|---|")
        for y, x in c.groupby("year"):
            print(f"| {y} | {len(x)} | {x['n'].mean():.0f} | {pct(x['ret'].mean())} | {pct((1 + x['ret']).prod() - 1, 0)} "
                  f"| {pct((1 + x['hedged']).prod() - 1, 0)} | {x['liq'].mean():.0%} |")
        print(flush=True)

    if a.part in ("events", "fade", "oi", "all"):
        ev = find_events(syms, d0, ranks, r4_min=0.20, vol_mult=5)
        fw = forward(syms, ev)
        fw.to_csv(a.save / "events.csv", index=False)

    if a.part in ("oi", "all"):
        ox = add_oi(fw)
        ox.to_csv(a.save / "events_oi.csv", index=False)
        ox = ox.dropna(subset=["oi_4h"])
        print(f"## 4. Open interest at the signal ({len(ox)} events with Binance metrics, Dec 2021 onward)\n")
        ox["oi4"] = pd.cut(ox.oi_4h, [-1, 0, 0.25, 0.5, 1, 1e9],
                           labels=["OI fell", "OI +0–25%", "OI +25–50%", "OI +50–100%", "OI more than doubled"])
        print("### by 4h open interest change (coin units, so price moves alone do not raise it)\n")
        print(fwd_table(ox, "oi4"))
        ox["ls"] = pd.cut(ox.ls_accounts, [0, 0.7, 1.0, 1.5, 1e9],
                          labels=["< 0.7 (most accounts short)", "0.7–1.0", "1.0–1.5", "> 1.5 (most accounts long)"])
        print("\n### by long/short account ratio\n")
        print(fwd_table(ox, "ls"))
        print(flush=True)

    if a.part in ("events", "all"):
        print("## 2. After a pump: 4h return >= +20% and hourly volume >= 5x its 7-day average\n")
        print("Returns are the coin's price change from the signal bar close (positive = bad for a short).\n")
        print(fwd_table(fw))
        fw["size"] = pd.cut(fw.r4, [0.2, 0.3, 0.5, 1.0, 100], labels=["+20–30%", "+30–50%", "+50–100%", "+100%+"])
        fw["funding"] = pd.cut(fw.fund, [-1, -0.0005, 0.0, 0.0001, 1],
                               labels=["< −0.05% (shorts crowded)", "−0.05–0%", "0–0.01% (neutral)", "> +0.01% (longs pay)"])
        fw["tier"] = pd.cut(fw["rank"], [0, 30, 100, 10_000], labels=["top 30", "31–100", "101+"])
        fw["age"] = pd.cut(fw.age_d, [-1, 30, 180, 1e9], labels=["listed < 30d", "30–180d", "> 180d"])
        fw["year"] = pd.to_datetime(fw.abs_h * 3600, unit="s").dt.year
        for col, title in [("size", "by pump size"), ("funding", "by funding at the signal"), ("tier", "by volume tier"),
                           ("age", "by coin age"), ("year", "by year")]:
            print(f"\n### {title}\n")
            print(fwd_table(fw, col))
        print(flush=True)

    if a.part in ("fade", "all"):
        print("\n## 3. Short the pump: 1x isolated, hold 7 days, entered at different delays\n")
        print("| entry | stop | trades | mean | median | win rate | liquidated | stopped |")
        print("|---|---|---|---|---|---|---|---|")
        for label, delay in [("at the signal", 1), ("+4h", 4), ("+24h", 24), ("+72h", 72)]:
            for stop in (None, 0.5, 0.3):
                res = []
                for e in fw.itertuples():
                    r = sim_short(syms[e.sym], e.h + delay, 168, 1.0, stop)
                    if r is not None:
                        res.append(r)
                if not res:
                    continue
                t = pd.DataFrame(res, columns=["ret", "why", "j"])
                print(f"| {label} | {'none' if stop is None else f'+{stop:.0%}'} | {len(t)} | {pct(t.ret.mean())} | {pct(t.ret.median())} "
                      f"| {(t.ret > 0).mean():.0%} | {(t.why == 'liq').mean():.1%} | {(t.why == 'stop').mean():.0%} |", flush=True)


if __name__ == "__main__":
    main()

"""Is "small caps drift down over time" true, and why is it hard to collect with shorts?

Every coin outside the top 100 by volume, started on the 1st of each month 2021-02..2025-10 and held a year:
how often it ends lower, how often it spikes first, and what a 1-year short earns at different leverage
(leverage below 1 = margin larger than the notional, which pushes the liquidation price up).
"""

import numpy as np
import pandas as pd

from short_study import H_DAY, load, sim_short, volume_ranks


def main() -> None:
    syms = load()
    d0, ranks, _ = volume_ranks(syms)
    starts = pd.date_range("2021-02-01", "2025-10-01", freq="MS")
    levs = (1.0, 0.5, 0.33, 0.25)
    price, shorts = [], {lev: [] for lev in levs}
    for name, s in syms.items():
        for m in starts:
            day = int(m.timestamp() // 86400)
            if day not in ranks.index or not ranks.at[day, name] > 100:
                continue
            i = day * H_DAY - s.t0
            if i < 0 or i >= s.n:
                continue
            end = min(i + 365 * H_DAY, s.n)
            if end < i + 365 * H_DAY and not s.delisted:
                continue
            p0 = s.open[i]
            price.append((s.close[end - 1] / p0 - 1, s.high[i:end].max() / p0 - 1))
            for lev in levs:
                r = sim_short(s, i, 365 * H_DAY, lev, None)
                if r:
                    shorts[lev].append((m, r[0], r[1]))
    t = pd.DataFrame(price, columns=["ret1y", "maxup"])
    lower = t[t.ret1y < 0]
    print(f"## Small caps held one year ({len(t)} coin-years)\n")
    print(f"- ended lower: {(t.ret1y < 0).mean():.0%}; lost more than half: {(t.ret1y < -0.5).mean():.0%}")
    print(f"- 1-year change: median {t.ret1y.median():+.0%}, mean {t.ret1y.mean():+.0%}; "
          f"95th percentile {t.ret1y.quantile(0.95):+.0%}, 99th {t.ret1y.quantile(0.99):+.0%}")
    print(f"- of the coins that ended lower, {(lower.maxup >= 0.5).mean():.0%} first rose 50%+ above the start "
          f"and {(lower.maxup >= 1).mean():.0%} rose 100%+\n")
    print("| short leverage | liquidated near | per position median / mean (on capital) | liquidated | basket by start month: median / worst / share positive |")
    print("|---|---|---|---|---|")
    for lev, v in shorts.items():
        x = pd.DataFrame(v, columns=["m", "ret", "why"])
        b = x.groupby("m").ret.mean()
        print(f"| {lev:g}x | +{(1 / lev + 1) / 1.025 - 1:.0%} | {x.ret.median():+.0%} / {x.ret.mean():+.1%} "
              f"| {(x.why == 'liq').mean():.0%} | {b.median():+.0%} / {b.min():+.0%} / {(b > 0).mean():.0%} |")


if __name__ == "__main__":
    main()

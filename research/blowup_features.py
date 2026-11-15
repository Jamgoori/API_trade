"""Which features at entry separate the chicken farm's blow-ups (liquidations) from the rest? Quintile tables.

Run after farm_filter.py: python3 research/blowup_features.py 2025_2026
"""
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import numpy as np, pandas as pd
import short_study as ss
from farm_filter import fetch_ls

tag = sys.argv[1]  # 2025_2026 or 2022_2025, from farm_filter.py
t = pd.read_csv(HERE / "results" / f"farm_filter_trades_{tag}.csv.gz")
t = t[t.rule == "no filter"].copy()
syms = ss.load()
d0, ranks, trailing = ss.volume_ranks(syms)
feat = []
for e in t.itertuples():
    s = syms[e.sym]; i = e.day * 24 - s.t0
    c = s.close
    r30 = c[i - 1] / c[i - 721] - 1 if i >= 721 else np.nan
    r7 = c[i - 1] / c[i - 169] - 1 if i >= 169 else np.nan
    lr = np.diff(np.log(c[max(i - 720, 0):i]))
    vol = lr.std() * np.sqrt(24 * 365) if len(lr) > 48 else np.nan
    fidx = np.flatnonzero(s.fund[max(i - 72, 0):i] != 0)
    fund3 = s.fund[max(i - 72, 0):i][fidx].mean() if len(fidx) else np.nan
    v7 = s.qvol[max(i - 168, 0):i].mean(); v30 = s.qvol[max(i - 720, 0):i].mean()
    hi30 = c[max(i - 720, 0):i].max(); dd = c[i - 1] / hi30 - 1
    feat.append((r30, r7, vol, fund3, v7 / v30 if v30 > 0 else np.nan, dd, i / 24, ranks.at[e.day, e.sym]))
f = pd.DataFrame(feat, columns=["ret_30d", "ret_7d", "vol_30d", "funding_3d", "volume_7d_vs_30d", "from_30d_high", "age_days", "volume_rank"], index=t.index)
ls = fetch_ls([(e.sym, e.day * 86400) for e in t.itertuples()])
f["ls_ratio"] = [ls.get((e.sym, e.day * 86400), np.nan) for e in t.itertuples()]
t = t.join(f)
t["liq"] = t.why == "liq"
print(f"{tag}: {len(t)} trades, liquidated {t.liq.mean():.1%}, mean {t.ret.mean():+.2%}\n")
for col in f.columns:
    q = pd.qcut(t[col], 5, duplicates="drop")
    g = t.groupby(q, observed=True).agg(n=("ret", "size"), liq=("liq", "mean"), mean=("ret", "mean"), median=("ret", "median"))
    print(f"{col}:")
    for k, r in g.iterrows():
        print(f"   {str(k):28s} n={r.n:5.0f} liq={r.liq:5.1%} mean={r['mean']:+6.1%} median={r['median']:+6.1%}")

"""Summarize results/tp_grid.csv: best combinations per period, how they did in the other period,
combinations that hold up in both, and the average effect of each parameter."""

from pathlib import Path

import pandas as pd

from short_study import pct

MIN_TRADES = 50
HERE = Path(__file__).resolve().parent


def label(r) -> str:
    ratio = f", ratio>{r.ratio:g}" if r.ratio else ""
    return (f"+{r.trigger:.0%} day{ratio}, TP −{r.tp:.0%}, stop +{r.stop:.0%}, {r.hold:.0f}d"
            + (", drop after stop" if r.ban else ""))


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="tp_grid.csv")
    ap.add_argument("--md", default="tp_grid.md")
    args = ap.parse_args()
    df = pd.read_csv(HERE / "results" / args.csv)
    keys = ["trigger", "ratio", "tp", "stop", "hold", "ban"]
    pa, pb = list(dict.fromkeys(df.period))[:2]
    a = df[df.period == pa].set_index(keys)
    b = df[df.period == pb].set_index(keys)
    w = a.join(b, lsuffix="_a", rsuffix="_b", how="inner").reset_index()
    w = w[(w.trades_a >= MIN_TRADES) & (w.trades_b >= MIN_TRADES)]
    out = [f"## Take-profit grid ({len(w)} combinations with at least {MIN_TRADES} trades in each period)\n",
           "Score: Calmar = portfolio CAGR / |max drawdown| (50 slots, 2% margin each, daily mark to market).\n"]

    def table(rows, title):
        out.append(f"### {title}\n")
        out.append(f"| combination | {pa} trades | per trade | CAGR | max DD | {pb} trades | per trade | CAGR | max DD |")
        out.append("|---|---|---|---|---|---|---|---|---|")
        for r in rows.itertuples():
            out.append(f"| {label(r)} | {r.trades_a} | {pct(r.mean_a)} | {pct(r.cagr_a, 0)} | {pct(r.maxdd_a, 0)} "
                       f"| {r.trades_b} | {pct(r.mean_b)} | {pct(r.cagr_b, 0)} | {pct(r.maxdd_b, 0)} |")
        out.append("")

    table(w.sort_values("calmar_a", ascending=False).head(10), f"Top 10 picked on {pa}, and how they did in {pb}")
    table(w.sort_values("calmar_b", ascending=False).head(10), f"Top 10 picked on {pb}, and how they did in {pa}")
    w["worst"] = w[["calmar_a", "calmar_b"]].min(axis=1)
    both = w[(w.cagr_a > 0) & (w.cagr_b > 0)]
    out.append(f"{len(both)} of {len(w)} combinations made money in both periods.\n")
    table(w.sort_values("worst", ascending=False).head(15), "Top 15 by the worse of the two periods")

    out.append("### Average effect of each setting (median Calmar across all other settings)\n")
    out.append(f"| setting | value | {pa} | {pb} | combos with CAGR > 0 in both |")
    out.append("|---|---|---|---|---|")
    for k in keys:
        for v, g in w.groupby(k):
            out.append(f"| {k} | {v} | {g.calmar_a.median():+.2f} | {g.calmar_b.median():+.2f} "
                       f"| {((g.cagr_a > 0) & (g.cagr_b > 0)).mean():.0%} |")
    (HERE / "results" / args.md).write_text("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    main()

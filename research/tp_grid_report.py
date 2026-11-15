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
    df = pd.read_csv(HERE / "results" / "tp_grid.csv")
    keys = ["trigger", "ratio", "tp", "stop", "hold", "ban"]
    a = df[df.period == "2022-24"].set_index(keys)
    b = df[df.period == "2025-26"].set_index(keys)
    w = a.join(b, lsuffix="_a", rsuffix="_b", how="inner").reset_index()
    w = w[(w.trades_a >= MIN_TRADES) & (w.trades_b >= MIN_TRADES)]
    out = [f"## Take-profit grid ({len(w)} combinations with at least {MIN_TRADES} trades in each period)\n",
           "Score: Calmar = portfolio CAGR / |max drawdown| (50 slots, 2% margin each, daily mark to market).\n"]

    def table(rows, title):
        out.append(f"### {title}\n")
        out.append("| combination | 2022-24 trades | per trade | CAGR | max DD | 2025-26 trades | per trade | CAGR | max DD |")
        out.append("|---|---|---|---|---|---|---|---|---|")
        for r in rows.itertuples():
            out.append(f"| {label(r)} | {r.trades_a} | {pct(r.mean_a)} | {pct(r.cagr_a, 0)} | {pct(r.maxdd_a, 0)} "
                       f"| {r.trades_b} | {pct(r.mean_b)} | {pct(r.cagr_b, 0)} | {pct(r.maxdd_b, 0)} |")
        out.append("")

    table(w.sort_values("calmar_b", ascending=False).head(10), "Top 10 picked on 2025-26, and how they did in 2022-24")
    table(w.sort_values("calmar_a", ascending=False).head(10), "Top 10 picked on 2022-24, and how they did in 2025-26")
    w["worst"] = w[["calmar_a", "calmar_b"]].min(axis=1)
    both = w[(w.cagr_a > 0) & (w.cagr_b > 0)]
    out.append(f"{len(both)} of {len(w)} combinations made money in both periods.\n")
    table(w.sort_values("worst", ascending=False).head(15), "Top 15 by the worse of the two periods")

    out.append("### Average effect of each setting (median Calmar across all other settings)\n")
    out.append("| setting | value | 2022-24 | 2025-26 | combos with CAGR > 0 in both |")
    out.append("|---|---|---|---|---|")
    for k in keys:
        for v, g in w.groupby(k):
            out.append(f"| {k} | {v} | {g.calmar_a.median():+.2f} | {g.calmar_b.median():+.2f} "
                       f"| {((g.cagr_a > 0) & (g.cagr_b > 0)).mean():.0%} |")
    (HERE / "results" / "tp_grid.md").write_text("\n".join(out) + "\n")
    print("\n".join(out))


if __name__ == "__main__":
    main()

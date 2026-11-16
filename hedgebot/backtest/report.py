"""Text rendering of backtest results."""

from __future__ import annotations

from datetime import datetime, timezone

from .sim import BacktestResult


def _d(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _money(x: float) -> str:
    return f"{'-' if x < 0 else ''}${abs(x):,.2f}"


def format_result(res: BacktestResult) -> str:
    p = res.params
    days = res.days
    lines = [
        f"Backtest {res.params.label or ''}".strip(),
        f"  period          {_d(res.start_ms)} -> {_d(res.end_ms)} UTC ({days:.1f} days)",
        f"  capital         " + ", ".join(f"{v} ${c:,.0f}" for v, c in res.start_equity.items()),
        f"  notional scale  x{p.notional_scale:g}" + (f", leverage {p.leverage_override:g}x" if p.leverage_override else ""),
        f"  rotation        every {p.min_hold_h:g}-{p.max_hold_h:g}h, {p.rotation_fraction_min:.0%}-{p.rotation_fraction_max:.0%}"
        f" of the position, window={p.rotate_when}, budget ${p.max_daily_cost_usd:g}/day",
        "",
        "  Result",
        f"    net PnL                {_money(res.net_pnl)}  ({_money(res.net_pnl / days)}/day)",
        f"      funding              {_money(res.funding)}",
        f"      fees                 {_money(-res.fees)}",
        f"      spread/slippage      {_money(-res.slippage)}",
        f"      basis & other        {_money(res.basis_and_other)}",
        f"    max drawdown           {_money(-res.max_drawdown_usd)}  ({res.max_drawdown_pct:.1f}% of peak equity)",
    ]
    for v in res.start_equity:
        lines.append(f"    {v:<22} volume ${res.venue_volume_per_day(v):,.0f}/day (both legs), "
                     f"average OI ${res.venue_avg_oi(v):,.0f}")
    if "arcus" in res.start_equity:
        lines += [
            f"    Arcus (maker) volume   ${res.maker_volume:,.0f}  (${res.maker_volume / days:,.0f}/day,"
            f" off-hours {res.maker_volume_off_hours / res.maker_volume:.0%})" if res.maker_volume else
            "    Arcus (maker) volume   $0",
            f"    est. Arcus points      {res.est_points:,.1f}  ({res.est_points / days * 7:,.1f}/week at "
            f"{p.points_per_musd:g} pts per $1M)",
            f"    cost per point         " + (f"${res.cost_per_point:,.2f}" if res.cost_per_point is not None else "n/a")
            + ("  (net positive: farming paid for itself)" if res.cost_per_point is not None and res.cost_per_point < 0
               else ""),
        ]
    lines += [
        f"    rotations / flips      {res.rotations} / {sum(r.flips for r in res.pairs.values())}",
        f"    de-risk / stops / liq  {res.derisks} / {res.stops} / {res.liquidations}",
        f"    min margin ratio       " + ", ".join(f"{v} {r:.2f}" for v, r in res.min_margin_ratio.items()),
        f"    venue transfers needed {res.transfers}  (${res.transfer_usd:,.0f} moved)",
        f"    isolated top-ups       {res.topups}  (${res.topup_usd:,.0f} moved into Entropy positions)",
        f"    scheduled rebalances   {res.rebalances}  (${res.rebalance_usd:,.0f} moved"
        + (f", every {p.rebalance_every_h:g}h)" if p.rebalance_every_h else ", off)"),
        f"    closest to isolated liq " + (", ".join(f"{v} {d:.1f}%" for v, d in res.min_iso_distance_pct.items())
                                         or "n/a"),
        "",
        "  Per pair",
        f"    {'pair':<14}{'maker vol':>12}{'off-hrs':>8}{'fees':>9}{'slip':>9}{'funding':>10}{'rot':>5}{'derisk':>7}"
        f"{'stop':>5}{'liq':>4}",
    ]
    for r in res.pairs.values():
        share = r.maker_volume_off_hours / r.maker_volume if r.maker_volume else 0
        lines.append(f"    {r.name:<14}{r.maker_volume:>12,.0f}{share:>8.0%}{r.fees:>9,.2f}{r.slippage:>9,.2f}"
                     f"{r.funding:>10,.2f}{r.rotations:>5}{r.derisks:>7}{r.stops:>5}{r.liquidations:>4}")
    groups: dict[str, list] = {}
    for name, r in res.pairs.items():
        groups.setdefault(res.groups.get(name) or "-", []).append(r)
    if len(groups) > 1 or "-" not in groups:
        lines.append("")
        lines.append("  Per group (volume per day: maker leg / hedge leg)")
        for g, rs in groups.items():
            mv = sum(r.maker_volume for r in rs) / days
            hv = sum(r.hedge_volume for r in rs) / days
            cost = sum(r.fees + r.slippage - r.funding for r in rs)
            lines.append(f"    {g:<6} ${mv:>10,.0f} / ${hv:>10,.0f}   net cost ${cost:,.2f}  "
                         f"(fees+spread-funding), avg OI ${sum(r.oi_hours for r in rs) / (days * 24):,.0f}")
    lines.append("")
    lines.append("  Equity by venue (end of day, UTC)")
    venues = list(res.start_equity)
    lines.append("    " + f"{'day':<12}" + "".join(f"{v:>12}" for v in venues) + f"{'total':>12}")
    for ts, eq in res.daily_equity[-10:]:
        lines.append("    " + f"{_d(ts)[:10]:<12}" + "".join(f"{eq.get(v, 0):>12,.0f}" for v in venues)
                     + f"{sum(eq.values()):>12,.0f}")
    return "\n".join(lines)


def format_sweep(results: list[BacktestResult]) -> str:
    """vol/day and avg OI count both legs on every venue; costs = fees + spread - funding."""
    header = (f"{'variant':<28}{'net PnL':>10}{'costs':>9}{'vol/day':>10}{'avg OI':>10}{'max DD':>9}{'DD%':>6}"
              f"{'derisk':>7}{'stop':>5}{'liq':>4}{'xfer':>5}{'min MR':>8}")
    lines = [header, "-" * len(header)]
    for r in results:
        mr = min((x for x in r.min_margin_ratio.values() if x == x), default=float("nan"))
        lines.append(f"{r.params.label:<28}{r.net_pnl:>10,.0f}{r.fees + r.slippage - r.funding:>9,.0f}"
                     f"{r.total_volume / r.days:>10,.0f}{r.total_avg_oi:>10,.0f}{r.max_drawdown_usd:>9,.0f}"
                     f"{r.max_drawdown_pct:>6.1f}{r.derisks:>7}{r.stops:>5}{r.liquidations:>4}{r.transfers:>5}{mr:>8.2f}")
    return "\n".join(lines)

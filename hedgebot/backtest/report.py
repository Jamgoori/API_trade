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
        f"    Arcus (maker) volume   ${res.maker_volume:,.0f}  (${res.maker_volume / days:,.0f}/day,"
        f" off-hours {res.maker_volume_off_hours / res.maker_volume:.0%})" if res.maker_volume else
        "    Arcus (maker) volume   $0",
        f"    hedge volume           ${res.hedge_volume:,.0f}",
        f"    open interest          ${res.oi_hours / (days * 24):,.0f} average maker-leg notional",
        f"    est. Arcus points      {res.est_points:,.1f}  ({res.est_points / days * 7:,.1f}/week at "
        f"{p.points_per_musd:g} pts per $1M)",
        f"    cost per point         " + (f"${res.cost_per_point:,.2f}" if res.cost_per_point is not None else "n/a")
        + ("  (net positive: farming paid for itself)" if res.cost_per_point is not None and res.cost_per_point < 0 else ""),
        f"    rotations / flips      {res.rotations} / {sum(r.flips for r in res.pairs.values())}",
        f"    de-risk / liquidations {res.derisks} / {res.liquidations}",
        f"    min margin ratio       " + ", ".join(f"{v} {r:.2f}" for v, r in res.min_margin_ratio.items()),
        f"    venue transfers needed {res.transfers}  (${res.transfer_usd:,.0f} moved)",
        "",
        "  Per pair",
        f"    {'pair':<14}{'maker vol':>12}{'off-hrs':>8}{'fees':>9}{'slip':>9}{'funding':>10}{'rot':>5}{'derisk':>7}{'liq':>4}",
    ]
    for r in res.pairs.values():
        share = r.maker_volume_off_hours / r.maker_volume if r.maker_volume else 0
        lines.append(f"    {r.name:<14}{r.maker_volume:>12,.0f}{share:>8.0%}{r.fees:>9,.2f}{r.slippage:>9,.2f}"
                     f"{r.funding:>10,.2f}{r.rotations:>5}{r.derisks:>7}{r.liquidations:>4}")
    lines.append("")
    lines.append("  Equity by venue (end of day, UTC)")
    venues = list(res.start_equity)
    lines.append("    " + f"{'day':<12}" + "".join(f"{v:>12}" for v in venues) + f"{'total':>12}")
    for ts, eq in res.daily_equity[-10:]:
        lines.append("    " + f"{_d(ts)[:10]:<12}" + "".join(f"{eq.get(v, 0):>12,.0f}" for v in venues)
                     + f"{sum(eq.values()):>12,.0f}")
    return "\n".join(lines)


def format_sweep(results: list[BacktestResult]) -> str:
    header = (f"{'variant':<28}{'net PnL':>11}{'$/day':>9}{'vol/day':>11}{'pts/wk':>8}{'$/pt':>8}"
              f"{'derisk':>7}{'liq':>4}{'xfer':>5}{'min MR':>8}")
    lines = [header, "-" * len(header)]
    for r in results:
        cpp = f"{r.cost_per_point:,.2f}" if r.cost_per_point is not None else "n/a"
        mr = min((x for x in r.min_margin_ratio.values() if x == x), default=float("nan"))
        lines.append(f"{r.params.label:<28}{r.net_pnl:>11,.0f}{r.net_pnl / r.days:>9,.1f}"
                     f"{r.maker_volume / r.days:>11,.0f}{r.est_points / r.days * 7:>8,.1f}{cpp:>8}"
                     f"{r.derisks:>7}{r.liquidations:>4}{r.transfers:>5}{mr:>8.2f}")
    return "\n".join(lines)

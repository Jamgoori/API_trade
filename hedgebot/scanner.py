"""Find Arcus RWA perps that can be hedged on Hyperliquid HIP-3 dexes, with live numbers."""

from __future__ import annotations

import csv
import logging
from dataclasses import asdict, dataclass
from decimal import Decimal

from . import strategy
from .util import HOURS_PER_YEAR, ZERO
from .venues.arcus import ArcusVenue
from .venues.base import VenueError
from .venues.hyperliquid import HyperliquidClient, HyperliquidVenue

log = logging.getLogger(__name__)

# Arcus ETF perps whose closest HIP-3 market tracks the same exposure at a different price scale.
PROXIES = {
    "GLD": ["GOLD"],
    "SLV": ["SILVER"],
    "SPY": ["SP500"],
    "QQQ": ["XYZ100"],
}


@dataclass
class Candidate:
    arcus: str
    hl: str
    kind: str  # same | proxy
    arcus_mid: Decimal
    hl_mid: Decimal
    basis_bps: Decimal
    arcus_spread_bps: Decimal
    hl_spread_bps: Decimal
    arcus_funding_apr: Decimal
    hl_funding_apr: Decimal
    best_direction: str
    carry_apr: Decimal
    arcus_max_lev: Decimal
    arcus_off_hours_lev: Decimal
    arcus_mmf: Decimal
    hl_max_lev: Decimal
    hl_isolated_only: bool
    hl_growth_mode: bool
    hl_taker_fee_bps: Decimal
    arcus_maker_fee_bps: Decimal
    arcus_vol_24h: Decimal
    hl_vol_24h: Decimal
    arcus_outside_rth: bool

    @property
    def score(self) -> Decimal:
        liquidity = min(self.arcus_vol_24h, self.hl_vol_24h)
        return liquidity / (1 + self.arcus_spread_bps + self.hl_spread_bps)


def scan(dexes: list[str], network: str = "mainnet", with_books: bool = True) -> list[Candidate]:
    arcus = ArcusVenue(network)
    a_markets = {m.symbol: m for m in arcus.refresh_markets().values() if m.is_rwa and m.online}
    client = HyperliquidClient(network, dexes=dexes)
    hl_markets = {}
    for dex in dexes:
        venue = HyperliquidVenue(client, dex)
        try:
            for m in venue.refresh_markets().values():
                if m.online:
                    hl_markets[m.symbol] = (venue, m)
        except VenueError as exc:
            log.warning("skipping dex %s: %s", dex, exc)

    out: list[Candidate] = []
    for a_sym, am in sorted(a_markets.items()):
        base = a_sym.split("-")[0]
        matches = [(f"{dex}:{base}", "same") for dex in dexes]
        matches += [(f"{dex}:{p}", "proxy") for dex in dexes for p in PROXIES.get(base, [])]
        for hl_sym, kind in matches:
            if hl_sym not in hl_markets:
                continue
            hv, hm = hl_markets[hl_sym]
            a_bbo = h_bbo = None
            if with_books:
                try:
                    a_bbo, h_bbo = arcus.bbo(a_sym), hv.bbo(hl_sym)
                except VenueError as exc:
                    log.info("no book for %s/%s: %s", a_sym, hl_sym, exc)
            a_mid = a_bbo.mid if a_bbo else am.mark_price
            h_mid = h_bbo.mid if h_bbo else hm.mark_price
            carry_long = strategy.carry_apr(1, am.funding_rate_1h, hm.funding_rate_1h)
            out.append(Candidate(
                arcus=a_sym, hl=hl_sym, kind=kind, arcus_mid=a_mid, hl_mid=h_mid,
                basis_bps=strategy.basis_bps(a_mid, h_mid) if kind == "same" and h_mid else ZERO,
                arcus_spread_bps=a_bbo.spread_bps if a_bbo else ZERO,
                hl_spread_bps=h_bbo.spread_bps if h_bbo else ZERO,
                arcus_funding_apr=am.funding_rate_1h * HOURS_PER_YEAR,
                hl_funding_apr=hm.funding_rate_1h * HOURS_PER_YEAR,
                best_direction="long_arcus" if carry_long >= 0 else "short_arcus",
                carry_apr=abs(carry_long),
                arcus_max_lev=am.max_leverage, arcus_off_hours_lev=am.off_hours_max_leverage,
                arcus_mmf=am.maintenance_margin_fraction, hl_max_lev=hm.max_leverage,
                hl_isolated_only=hm.isolated_only, hl_growth_mode=bool(hm.extra.get("growthMode")),
                hl_taker_fee_bps=hm.taker_fee * 10000, arcus_maker_fee_bps=am.maker_fee * 10000,
                arcus_vol_24h=am.volume_24h_usd, hl_vol_24h=hm.volume_24h_usd,
                arcus_outside_rth=am.outside_rth,
            ))
    out.sort(key=lambda c: c.score, reverse=True)
    return out


def format_table(rows: list[Candidate]) -> str:
    header = (f"{'Arcus':<10} {'Hyperliquid':<14} {'kind':<5} {'basis':>7} {'spr A/H bps':>12} {'fund A/H %apr':>14} "
              f"{'carry':>6} {'dir':<11} {'lev A(off)/H':>13} {'mmfA':>5} {'iso':>3} {'feeH':>5} {'vol24h A/H $M':>14}")
    lines = [header, "-" * len(header)]
    for c in rows:
        lines.append(
            f"{c.arcus:<10} {c.hl:<14} {c.kind:<5} {c.basis_bps:>7.1f} "
            f"{c.arcus_spread_bps:>5.1f}/{c.hl_spread_bps:<6.1f} "
            f"{c.arcus_funding_apr * 100:>6.1f}/{c.hl_funding_apr * 100:<7.1f} {c.carry_apr * 100:>5.1f}% "
            f"{c.best_direction:<11} {c.arcus_max_lev:>4.0f}({c.arcus_off_hours_lev:>3.0f})/{c.hl_max_lev:<4.0f} "
            f"{c.arcus_mmf * 100:>4.1f}% {'Y' if c.hl_isolated_only else '-':>3} {c.hl_taker_fee_bps:>4.1f} "
            f"{c.arcus_vol_24h / 1_000_000:>6.1f}/{c.hl_vol_24h / 1_000_000:<7.1f}"
        )
    return "\n".join(lines)


def write_csv(rows: list[Candidate], path: str) -> None:
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(asdict(rows[0]).keys()) if rows else ["arcus"])
        writer.writeheader()
        for c in rows:
            writer.writerow({k: str(v) for k, v in asdict(c).items()})

"""Bar-level portfolio backtest of the hedge farming strategy.

It replays the same rules the live executor uses (direction choice, build / hold /
rotate cycle, basis guard, off-hours rotation window, daily cost budget, margin
limits, de-risking) on historical candles and funding of both legs, with every
pair sharing the per-venue margin accounts.

What it can tell you: funding carry, cross-venue basis risk, how often margin gets
tight (and how often you would have had to move money between venues),
liquidation / de-risk events at a given leverage, the volume a rotation schedule
produces and what that volume costs.

What it cannot: whether a post-only order would really have filled (no historical
order books), exact spreads (an assumed half-spread per venue is used), and the
real points formula (an assumed points-per-$1M rate converts volume to points).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .. import strategy
from ..marketcal import is_arcus_off_hours
from .data import PairData

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
BASIS_HALF_LIFE_MS = 1_800_000


@dataclass
class LegSpec:
    venue: str
    maker_fee: float
    taker_fee: float
    mmf: float
    max_lev: float
    off_hours_lev: float
    isolated: bool
    half_spread_bps: float
    half_spread_off_bps: float
    liquidation_fee: float = 0.01


@dataclass
class BacktestParams:
    capital: dict[str, float]
    leg_specs: dict[tuple[str, str], LegSpec]  # (venue, symbol) -> spec
    notional_scale: float = 1.0
    leverage_override: float | None = None
    min_hold_h: float = 3
    max_hold_h: float = 8
    rotation_fraction_min: float = 0.3
    rotation_fraction_max: float = 0.7
    rotate_when: str = "off_hours"
    max_daily_cost_usd: float = 20
    basis_guard_bps: float = 8
    use_basis_guard: bool = True
    target_tolerance: float = 0.08
    min_carry_edge_apr: float = 0.03
    flip_carry_apr: float = 0.08
    funding_window_h: float = 168
    margin_ratio_warn: float = 3.0
    margin_ratio_critical: float = 1.7
    derisk_fraction: float = 0.3
    isolated_liq_buffer_critical_pct: float = 4
    isolated_liq_buffer_warn_pct: float = 8
    isolated_topup_enabled: bool = True
    isolated_topup_target_pct: float = 15
    rotation_enabled: bool = True  # False disables rotation for every pair (hold-only variant)
    max_account_leverage: float = 5
    hedge_slippage_bps: float = 0.3
    maker_adverse_bps: float = 0.5
    # passive_first hedging: share of hedge volume that fills as maker at the touch; the rest is crossed
    # after the timeout, assumed to have drifted `passive_miss_adverse_bps` against us meanwhile.
    passive_hedge_fill_rate: float = 0.0
    passive_miss_adverse_bps: float = 1.5
    urgent_slippage_bps: float = 10.0
    allow_transfers: bool = True
    backstop_enabled: bool = True
    backstop_margin_ratio: float = 1.25
    backstop_isolated_buffer_pct: float = 2.0
    stop_cooldown_h: float = 6
    points_per_musd: float = 33
    seed: int = 7
    label: str = ""


@dataclass
class PairResult:
    name: str
    maker_volume: float = 0.0
    maker_volume_off_hours: float = 0.0
    hedge_volume: float = 0.0
    fees: float = 0.0
    slippage: float = 0.0
    funding: float = 0.0
    rotations: int = 0
    flips: int = 0
    derisks: int = 0
    stops: int = 0
    liquidations: int = 0
    topups: int = 0
    topup_usd: float = 0.0
    oi_hours: float = 0.0
    final_qty: float = 0.0
    direction_changes: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class BacktestResult:
    params: BacktestParams
    start_ms: int
    end_ms: int
    pairs: dict[str, PairResult]
    start_equity: dict[str, float]
    end_equity: dict[str, float]
    min_margin_ratio: dict[str, float]
    transfers: int
    transfer_usd: float
    daily_equity: list[tuple[int, dict[str, float]]]
    groups: dict[str, str] = field(default_factory=dict)

    @property
    def days(self) -> float:
        return max((self.end_ms - self.start_ms) / DAY_MS, 1e-9)

    @property
    def net_pnl(self) -> float:
        return sum(self.end_equity.values()) - sum(self.start_equity.values())

    @property
    def maker_volume(self) -> float:
        return sum(p.maker_volume for p in self.pairs.values())

    @property
    def maker_volume_off_hours(self) -> float:
        return sum(p.maker_volume_off_hours for p in self.pairs.values())

    @property
    def hedge_volume(self) -> float:
        return sum(p.hedge_volume for p in self.pairs.values())

    @property
    def fees(self) -> float:
        return sum(p.fees for p in self.pairs.values())

    @property
    def slippage(self) -> float:
        return sum(p.slippage for p in self.pairs.values())

    @property
    def funding(self) -> float:
        return sum(p.funding for p in self.pairs.values())

    @property
    def basis_and_other(self) -> float:
        """PnL not explained by funding and execution costs: basis moves, liquidation losses."""
        return self.net_pnl - self.funding + self.fees + self.slippage

    @property
    def oi_hours(self) -> float:
        return sum(p.oi_hours for p in self.pairs.values())

    @property
    def est_points(self) -> float:
        return self.maker_volume / 1e6 * self.params.points_per_musd

    @property
    def cost_per_point(self) -> float | None:
        if self.est_points <= 0:
            return None
        return -self.net_pnl / self.est_points

    @property
    def liquidations(self) -> int:
        return sum(p.liquidations for p in self.pairs.values())

    @property
    def derisks(self) -> int:
        return sum(p.derisks for p in self.pairs.values())

    @property
    def stops(self) -> int:
        return sum(p.stops for p in self.pairs.values())

    @property
    def topups(self) -> int:
        return sum(p.topups for p in self.pairs.values())

    @property
    def topup_usd(self) -> float:
        return sum(p.topup_usd for p in self.pairs.values())

    @property
    def rotations(self) -> int:
        return sum(p.rotations for p in self.pairs.values())


class _Pair:
    def __init__(self, data: PairData, params: BacktestParams, timeline: list[int]):
        self.data = data
        self.cfg = data.pair
        self.name = data.pair.name
        self.mv, self.hv = data.pair.maker.venue, data.pair.hedge.venue
        self.ms = params.leg_specs[(self.mv, data.pair.maker.symbol)]
        self.hs = params.leg_specs[(self.hv, data.pair.hedge.symbol)]
        self.ratio = float(data.pair.hedge_ratio)
        self.notional = float(data.pair.notional_usd) * params.notional_scale
        self.leverage = params.leverage_override or float(data.pair.leverage)
        pc = data.pair
        self.rotate = params.rotation_enabled and pc.rotate
        self.min_hold_h = pc.min_hold_h if pc.min_hold_h is not None else params.min_hold_h
        self.max_hold_h = pc.max_hold_h if pc.max_hold_h is not None else params.max_hold_h
        self.frac_min = float(pc.rotation_fraction_min) if pc.rotation_fraction_min is not None \
            else params.rotation_fraction_min
        self.frac_max = float(pc.rotation_fraction_max) if pc.rotation_fraction_max is not None \
            else params.rotation_fraction_max
        self.rotate_when = pc.rotate_when or params.rotate_when
        self.iso_m = 0.0  # isolated margin posted to each leg (when that leg's market is isolated)
        self.iso_h = 0.0
        self.m_idx = _align(data.maker.t, timeline)
        self.h_idx = _align(data.hedge.t, timeline)
        self.direction = 0
        self.qty = 0.0  # maker units; the hedge leg holds -qty * ratio
        self.entry_m = 0.0
        self.entry_h = 0.0
        self.target = 0.0
        self.phase = "build"
        self.next_rot = 0
        self.derisk_until = 0
        self.basis_ref: float | None = None
        self.basis_t = 0
        self.prev_t: int | None = None
        self.prev_m = self.prev_h = 0.0
        self.fptr_m = [0]
        self.fptr_h = [0]
        self.res = PairResult(self.name)

    def active(self, i: int) -> bool:
        return self.m_idx[i] >= 0 and self.h_idx[i] >= 0

    def bar(self, i: int):
        mi, hi = self.m_idx[i], self.h_idx[i]
        m, h = self.data.maker, self.data.hedge
        return (m.high[mi], m.low[mi], m.close[mi]), (h.high[hi], h.low[hi], h.close[hi])

    def fresh(self, i: int, timeline: list[int]) -> bool:
        """Both legs printed a bar at this step (no forward-filled stale close)."""
        t = timeline[i]
        return self.data.maker.t[self.m_idx[i]] == t and self.data.hedge.t[self.h_idx[i]] == t


def _iso_liq(size: float, entry: float, margin: float, mmf: float) -> float | None:
    """Liquidation price of an isolated position (see venues.arcus.isolated_liquidation_price)."""
    if size == 0 or margin <= 0:
        return None
    denom = size * (1 - mmf) if size > 0 else size * (1 + mmf)
    return max((size * entry - margin) / denom, 0.0)


def _align(series_t: list[int], timeline: list[int]) -> list[int]:
    """Index of the latest bar at or before each timeline step (-1 before the first bar)."""
    out, j = [], -1
    for t in timeline:
        while j + 1 < len(series_t) and series_t[j + 1] <= t:
            j += 1
        out.append(j)
    return out


def _rate_at(rows: list[tuple[int, float]], t: int, ptr: list[int]) -> float:
    k = ptr[0]
    while k + 1 < len(rows) and rows[k + 1][0] <= t:
        k += 1
    ptr[0] = k
    return rows[k][1] if rows and rows[k][0] <= t else 0.0


def _trailing_avg(rows: list[tuple[int, float]], t: int, window_ms: int = DAY_MS) -> float:
    vals = [r for ts, r in rows if t - window_ms < ts <= t]
    return sum(vals) / len(vals) if vals else 0.0


def run_backtest(datas: list[PairData], params: BacktestParams, start_ms: int, end_ms: int) -> BacktestResult:
    rng = random.Random(params.seed)
    fwin = int(params.funding_window_h * HOUR_MS)
    timeline = sorted({t for d in datas for t in (*d.maker.t, *d.hedge.t) if start_ms <= t <= end_ms})
    pairs = [_Pair(d, params, timeline) for d in datas]
    venues = sorted({p.mv for p in pairs} | {p.hv for p in pairs})
    equity = {v: float(params.capital.get(v, 0.0)) for v in venues}
    start_equity = dict(equity)
    min_ratio = {v: math.inf for v in venues}
    totals = {"transfers": 0, "transfer_usd": 0.0}
    daily: list[tuple[int, dict[str, float]]] = []
    day_cost: dict[int, float] = {}
    state = {"off": False}

    def legs_on(venue: str):
        """(pair, signed size, spec, is_maker) for every open leg on a venue."""
        out = []
        for p in pairs:
            if p.qty == 0:
                continue
            if p.mv == venue:
                out.append((p, p.qty, p.ms, True))
            if p.hv == venue:
                out.append((p, -p.qty * p.ratio, p.hs, False))
        return out

    def leg_lev(p: "_Pair", spec: LegSpec, venue: str, new_risk: bool) -> float:
        venue_max = spec.off_hours_lev if (new_risk and venue == "arcus" and state["off"]) else spec.max_lev
        return max(1.0, min(p.leverage, venue_max))

    def margin_state(venue: str, i: int, new_risk: bool = False):
        gross = maint = initial = 0.0
        for p, size, spec, is_maker in legs_on(venue):
            (_, _, mc), (_, _, hc) = p.bar(i)
            notional = abs(size) * (mc if is_maker else hc)
            gross += notional
            maint += notional * spec.mmf
            if spec.isolated:
                initial += p.iso_m if is_maker else p.iso_h
            else:
                initial += notional / leg_lev(p, spec, venue, new_risk)
        return gross, maint, initial

    def addable(venue: str, p: "_Pair", i: int) -> float:
        gross, _, initial = margin_state(venue, i, new_risk=True)
        spec = p.ms if venue == p.mv else p.hs
        lev = leg_lev(p, spec, venue, True)
        return max(0.0, min((equity[venue] - initial) * lev * 0.9,
                            equity[venue] * params.max_account_leverage - gross))

    def book_cost(p: "_Pair", t: int, m_fee: float, h_fee: float, m_slip: float, h_slip: float) -> None:
        equity[p.mv] -= m_fee + m_slip
        equity[p.hv] -= h_fee + h_slip
        p.res.fees += m_fee + h_fee
        p.res.slippage += m_slip + h_slip
        day = t // DAY_MS
        day_cost[day] = day_cost.get(day, 0.0) + m_fee + h_fee + max(0.0, m_slip) + max(0.0, h_slip)

    def trade(p: "_Pair", delta: float, i: int, t: int, urgent: bool = False) -> None:
        """Move the maker leg by delta (the hedge by -delta*ratio) at the bar close."""
        if delta == 0:
            return
        (_, _, mc), (_, _, hc) = p.bar(i)
        side = 1 if delta > 0 else -1
        off_now = state["off"]
        hs_m = p.ms.half_spread_off_bps if off_now else p.ms.half_spread_bps
        hs_h = p.hs.half_spread_off_bps if off_now else p.hs.half_spread_bps
        if urgent:  # both legs cross the spread
            m_px = mc * (1 + side * (hs_m + params.urgent_slippage_bps) / 1e4)
            m_fee_rate = p.ms.taker_fee
        else:  # maker fill at mid, minus adverse selection
            m_px = mc * (1 + side * params.maker_adverse_bps / 1e4)
            m_fee_rate = p.ms.maker_fee
        h_delta = -delta * p.ratio
        h_side = 1 if h_delta > 0 else -1
        passive = 0.0 if urgent else params.passive_hedge_fill_rate
        taker_bps = hs_h + params.hedge_slippage_bps + (params.urgent_slippage_bps if urgent else 0.0)
        if passive:
            taker_bps += params.passive_miss_adverse_bps
        h_bps = passive * -hs_h + (1 - passive) * taker_bps
        h_px = hc * (1 + h_side * h_bps / 1e4)
        h_fee_rate = passive * p.hs.maker_fee + (1 - passive) * p.hs.taker_fee
        m_notional, h_notional = abs(delta) * m_px, abs(h_delta) * h_px
        book_cost(p, t, m_notional * m_fee_rate, h_notional * h_fee_rate,
                  abs(delta) * (m_px - mc) * side, abs(h_delta) * (h_px - hc) * h_side)
        p.res.maker_volume += m_notional
        if off_now:
            p.res.maker_volume_off_hours += m_notional
        p.res.hedge_volume += h_notional
        new_qty = p.qty + delta
        for is_maker, spec, venue, old_sz, new_sz, px in (
            (True, p.ms, p.mv, p.qty, new_qty, mc),
            (False, p.hs, p.hv, -p.qty * p.ratio, -new_qty * p.ratio, hc),
        ):
            if not spec.isolated:
                continue
            m = p.iso_m if is_maker else p.iso_h
            lev = leg_lev(p, spec, venue, False)
            if new_sz == 0:
                m = 0.0
            elif old_sz == 0 or (old_sz > 0) != (new_sz > 0):
                m = abs(new_sz) * px / lev
            elif abs(new_sz) > abs(old_sz):
                m += (abs(new_sz) - abs(old_sz)) * px / lev
            else:
                m *= abs(new_sz) / abs(old_sz)
            if is_maker:
                p.iso_m = m
            else:
                p.iso_h = m
        if p.qty == 0 or (p.qty > 0) == (delta > 0):
            if new_qty:
                p.entry_m = (p.entry_m * abs(p.qty) + mc * abs(delta)) / abs(new_qty)
                p.entry_h = (p.entry_h * abs(p.qty) + hc * abs(delta)) / abs(new_qty)
        p.qty = new_qty
        if abs(p.qty) * mc < 1e-6:
            p.qty = 0.0

    def derisk(p: "_Pair", i: int, t: int) -> None:
        trade(p, -p.qty * params.derisk_fraction, i, t, urgent=True)
        p.target = p.qty
        p.phase = "hold"
        p.derisk_until = t + 6 * HOUR_MS
        p.next_rot = t + int(rng.uniform(p.min_hold_h, p.max_hold_h) * HOUR_MS)
        p.res.derisks += 1

    def liquidate(p: "_Pair", maker_leg: bool, extreme: float, i: int, t: int, stop: bool = False) -> None:
        """One leg is closed by the venue at the bar extreme (liquidation, or our exchange stop when
        `stop`: no liquidation penalty, just a market fill); the bot closes the other leg at the close."""
        (_, _, mc), (_, _, hc) = p.bar(i)
        spec, venue = (p.ms, p.mv) if maker_leg else (p.hs, p.hv)
        size = p.qty if maker_leg else -p.qty * p.ratio
        close = mc if maker_leg else hc
        penalty = (params.urgent_slippage_bps / 1e4 + spec.taker_fee) if stop else spec.liquidation_fee
        equity[venue] += size * (extreme - close) - abs(size) * extreme * penalty
        other_spec, other_venue = (p.hs, p.hv) if maker_leg else (p.ms, p.mv)
        other_size = -p.qty * p.ratio if maker_leg else p.qty
        other_close = hc if maker_leg else mc
        cost = abs(other_size) * other_close * ((other_spec.half_spread_off_bps + params.urgent_slippage_bps) / 1e4
                                                 + other_spec.taker_fee)
        equity[other_venue] -= cost
        p.res.fees += cost
        p.qty = 0.0
        p.iso_m = p.iso_h = 0.0
        p.phase = "build"
        p.direction = 0
        p.derisk_until = t + int((params.stop_cooldown_h if stop else 6) * HOUR_MS)
        if stop:
            p.res.stops += 1
        else:
            p.res.liquidations += 1

    last_day = None
    for i, t in enumerate(timeline):
        state["off"] = off = is_arcus_off_hours(t)
        day = t // DAY_MS
        if last_day is not None and day != last_day:
            daily.append((last_day * DAY_MS, dict(equity)))
        last_day = day

        # 1) mark to market and funding
        for p in pairs:
            if not p.active(i):
                continue
            (_, _, mc), (_, _, hc) = p.bar(i)
            if p.prev_t is None:
                p.prev_t, p.prev_m, p.prev_h = t, mc, hc
                continue
            if p.qty:
                h_size = -p.qty * p.ratio
                equity[p.mv] += p.qty * (mc - p.prev_m)
                equity[p.hv] += h_size * (hc - p.prev_h)
                p.res.oi_hours += abs(p.qty) * mc * (t - p.prev_t) / HOUR_MS
                for hour in range(p.prev_t // HOUR_MS + 1, t // HOUR_MS + 1):
                    ht = hour * HOUR_MS
                    pay_m = -p.qty * mc * _rate_at(p.data.maker_funding, ht, p.fptr_m)
                    pay_h = -h_size * hc * _rate_at(p.data.hedge_funding, ht, p.fptr_h)
                    equity[p.mv] += pay_m
                    equity[p.hv] += pay_h
                    p.res.funding += pay_m + pay_h
            p.prev_t, p.prev_m, p.prev_h = t, mc, hc

        # 2) isolated legs: exchange stop / liquidation on the bar's adverse extreme, then top-up or de-risk
        for p in pairs:
            if not p.active(i) or not p.qty:
                continue
            (mh, ml, mc), (hh, hl, hc) = p.bar(i)
            for maker_leg, spec, venue, size, entry, extreme, close in (
                (True, p.ms, p.mv, p.qty, p.entry_m, ml if p.qty > 0 else mh, mc),
                (False, p.hs, p.hv, -p.qty * p.ratio, p.entry_h, hl if p.qty < 0 else hh, hc),
            ):
                if not spec.isolated or entry <= 0 or not p.qty:
                    continue
                margin = p.iso_m if maker_leg else p.iso_h
                liq = _iso_liq(size, entry, margin, spec.mmf)
                if liq is None:
                    continue
                worst = (extreme - liq) / extreme if size > 0 else (liq - extreme) / extreme
                if params.backstop_enabled and worst <= params.backstop_isolated_buffer_pct / 100:
                    liquidate(p, maker_leg, extreme, i, t, stop=True)
                    continue
                if worst <= 0:
                    liquidate(p, maker_leg, extreme, i, t)
                    continue
                dist = (close - liq) / close if size > 0 else (liq - close) / close
                if dist * 100 < params.isolated_liq_buffer_warn_pct and params.isolated_topup_enabled:
                    target = params.isolated_topup_target_pct / 100
                    if size > 0:
                        need = (liq - close * (1 - target)) * size * (1 - spec.mmf)
                    else:
                        need = (close * (1 + target) - liq) * abs(size) * (1 + spec.mmf)
                    free = equity[venue] - margin_state(venue, i)[2]
                    add = min(need, free * 0.95)
                    if add >= 5:
                        if maker_leg:
                            p.iso_m += add
                        else:
                            p.iso_h += add
                        p.res.topups += 1
                        p.res.topup_usd += add
                        liq = _iso_liq(size, entry, margin + add, spec.mmf)
                        dist = (close - liq) / close if size > 0 else (liq - close) / close
                if dist * 100 < params.isolated_liq_buffer_critical_pct and t >= p.derisk_until:
                    derisk(p, i, t)

        # 3) cross accounts: worst equity inside the bar versus maintenance
        for v in venues:
            legs = [leg for leg in legs_on(v) if not leg[2].isolated]
            if not legs:
                continue
            worst_eq, worst_maint = equity[v], 0.0
            for p, size, spec, is_maker in legs:
                (mh, ml, mc), (hh, hl, hc) = p.bar(i)
                hi_, lo_, cl = (mh, ml, mc) if is_maker else (hh, hl, hc)
                extreme = lo_ if size > 0 else hi_
                worst_eq += size * (extreme - cl)
                worst_maint += abs(size) * extreme * spec.mmf
            if worst_maint <= 0:
                continue
            ratio = worst_eq / worst_maint
            min_ratio[v] = min(min_ratio[v], ratio)
            stop_hit = params.backstop_enabled and ratio < params.backstop_margin_ratio
            if ratio < 1.0 or stop_hit:
                for p, size, spec, is_maker in legs:
                    (mh, ml, _), (hh, hl, _) = p.bar(i)
                    hi_, lo_ = (mh, ml) if is_maker else (hh, hl)
                    if p.qty:
                        liquidate(p, is_maker, lo_ if size > 0 else hi_, i, t, stop=stop_hit)
            elif ratio < params.margin_ratio_critical:
                for p, _, _, _ in legs:
                    if p.qty and t >= p.derisk_until:
                        derisk(p, i, t)
            elif ratio < params.margin_ratio_warn and params.allow_transfers:
                _, maint, _ = margin_state(v, i)
                need = maint * (params.margin_ratio_warn + 1) - equity[v]
                donors = []
                for d in venues:
                    if d != v:
                        spare = equity[d] - margin_state(d, i)[1] * (params.margin_ratio_warn + 1)
                        if spare > 0:
                            donors.append((spare, d))
                if need > 0 and donors:
                    spare, d = max(donors)
                    amount = min(need, spare)
                    equity[d] -= amount
                    equity[v] += amount
                    totals["transfers"] += 1
                    totals["transfer_usd"] += amount

        # 4) strategy decisions at the bar close
        venue_bias: dict[str, float] = {}
        for p in pairs:
            if not p.active(i) or p.prev_t is None:
                continue
            (_, _, mc), (_, _, hc) = p.bar(i)
            if p.fresh(i, timeline):
                basis = (mc - hc) / hc * 1e4
                dt = t - p.basis_t if p.basis_ref is not None else 0
                alpha = 1 - 0.5 ** (dt / BASIS_HALF_LIFE_MS)
                p.basis_ref = basis if p.basis_ref is None else p.basis_ref + alpha * (basis - p.basis_ref)
                p.basis_t = t
            else:
                basis = None  # a stale close on one leg says nothing about the basis

            if p.direction == 0:
                p.direction = strategy.choose_direction(
                    p.cfg.direction, _trailing_avg(p.data.maker_funding, t, fwin), _trailing_avg(p.data.hedge_funding, t, fwin),
                    params.min_carry_edge_apr, venue_bias.get(p.mv, 0.0))
                p.target = p.direction * p.notional / mc
                p.res.direction_changes.append((t, p.direction))
            venue_bias[p.mv] = venue_bias.get(p.mv, 0.0) + p.direction * p.notional

            def guard_ok(side: int) -> bool:
                if not params.use_basis_guard:
                    return basis is not None
                if basis is None or p.basis_ref is None:
                    return False
                edge = (p.basis_ref - basis) if side > 0 else (basis - p.basis_ref)
                return edge >= -params.basis_guard_bps

            tol = params.target_tolerance
            if p.phase == "build":
                if t < p.derisk_until and abs(p.target) > abs(p.qty):
                    continue
                if p.target and abs(p.qty / p.target - 1) <= tol:
                    p.phase = "hold"
                    p.next_rot = t + int(rng.uniform(p.min_hold_h, p.max_hold_h) * HOUR_MS)
                    continue
                gap = p.target - p.qty
                side = 1 if gap > 0 else -1
                if not guard_ok(side):
                    continue
                if abs(p.qty + gap) > abs(p.qty):
                    room = min(addable(p.mv, p, i), addable(p.hv, p, i) / p.ratio)
                    gap = side * min(abs(gap), room / mc)
                if abs(gap) * mc >= 15:
                    trade(p, gap, i, t)
            elif p.phase == "hold":
                if p.target and abs(p.qty / p.target - 1) > 2 * tol and t >= p.derisk_until:
                    p.phase = "build"
                    p.target = p.direction * p.notional / mc
                    continue
                if not p.rotate or t < p.next_rot or not strategy.rotation_window_open(p.rotate_when, off):
                    continue
                if day_cost.get(day, 0.0) >= params.max_daily_cost_usd:
                    p.next_rot = t + HOUR_MS // 2
                    continue
                flip = p.cfg.direction == "auto" and strategy.should_flip(
                    p.direction, _trailing_avg(p.data.maker_funding, t, fwin), _trailing_avg(p.data.hedge_funding, t, fwin),
                    params.flip_carry_apr)
                frac = 1.0 if flip else rng.uniform(p.frac_min, p.frac_max)
                if not guard_ok(-1 if p.qty > 0 else 1):
                    p.next_rot = t + HOUR_MS // 4
                    continue
                trade(p, -p.qty * frac, i, t)
                if flip:
                    p.direction = -p.direction
                    p.res.flips += 1
                    p.res.direction_changes.append((t, p.direction))
                p.target = p.direction * p.notional / mc
                p.phase = "build"
                p.res.rotations += 1

    if last_day is not None:
        daily.append((last_day * DAY_MS, dict(equity)))
    for p in pairs:
        p.res.final_qty = p.qty
    return BacktestResult(
        params=params,
        start_ms=timeline[0] if timeline else start_ms,
        end_ms=timeline[-1] if timeline else end_ms,
        pairs={p.name: p.res for p in pairs},
        start_equity=start_equity,
        end_equity=dict(equity),
        min_margin_ratio={v: (r if r != math.inf else float("nan")) for v, r in min_ratio.items()},
        transfers=totals["transfers"],
        transfer_usd=totals["transfer_usd"],
        daily_equity=daily,
        groups={p.name: p.cfg.group for p in pairs},
    )

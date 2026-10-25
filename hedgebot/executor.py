"""Per-pair execution: maker clip on the maker venue, immediate taker hedge on the other.

Life cycle of a pair (persisted in the journal so restarts resume where they left off):

    BUILD  -> work the maker leg towards the target, hedging every fill at once
    HOLD   -> keep the hedged position (open interest + deposits) until the next rotation
    UNWIND -> reduce by a random fraction (or to zero for a direction flip), then BUILD again

Every maker fill is hedged immediately with an IOC on the hedge venue, escalating
through the configured slippage ladder. If the hedge cannot be completed, the
unhedged maker quantity is closed with an IOC instead - the bot never sits on a
naked position by choice. An independent imbalance check repairs any residual
difference between the legs by shrinking the larger leg (reduce-only).
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Protocol

from . import strategy
from .config import Config, PairConfig
from .marketcal import minutes_to_session_boundary
from .models import BBO, AccountState, MarketInfo, OrderState, OrderStatus, Side, Tif
from .util import D, ZERO, now_ms
from .venues.base import Venue, VenueError

log = logging.getLogger(__name__)

SETTLE_S = 2.5  # let venue position stores catch up after a clip before judging the legs again
CANCEL_RETRY_S = 20
REJECT_COOLDOWN_S = 30
HEDGE_FAILURE_COOLDOWN_S = 600
BASIS_HALF_LIFE_S = 1800


class Phase(str, Enum):
    BUILD = "build"
    HOLD = "hold"
    UNWIND = "unwind"


class Context(Protocol):
    """What the executor needs from the engine (a fake implements it in tests)."""

    cfg: Config
    venues: dict[str, Venue]
    halted: bool

    def account(self, venue: str, max_age_s: float = ...) -> AccountState: ...
    def invalidate_account(self, venue: str) -> None: ...
    def take_forced_reduction(self, pair: str) -> Decimal | None: ...
    def rotation_budget_left(self) -> bool: ...
    def funding_avg(self, venue: str, symbol: str) -> Decimal: ...
    def maker_venue_bias(self, pair: str, venue: str) -> Decimal: ...
    def journal_fill(self, **kwargs) -> None: ...
    def journal_order(self, venue: str, pair: str, purpose: str, order: OrderState) -> None: ...
    def event(self, level: str, kind: str, message: str, *, pair: str | None = None, venue: str | None = None,
              data: dict | None = None, notify: bool = False, throttle_key: str | None = None,
              throttle_s: float = 0) -> None: ...


@dataclass
class Runtime:
    """Persisted pair state."""

    direction: int = 0
    phase: str = Phase.BUILD.value
    target_qty: str = "0"
    unwind_to: str = "0"
    next_rotation_ts: float = 0.0
    rotations_done: int = 0
    flip_pending: bool = False
    derisk_until: float = 0.0

    @property
    def target(self) -> Decimal:
        return D(self.target_qty)


@dataclass
class Clip:
    purpose: str  # build | unwind | derisk
    maker_side: Side
    hedge_side: Side
    order: OrderState
    maker_mid: Decimal
    outside_rth: bool
    urgent: bool
    filled_seen: Decimal = ZERO
    hedge_pending: Decimal = ZERO  # hedge-leg units still to hedge
    last_fill_ts: float = 0.0
    cancel_requested: bool = False
    cancel_ts: float = 0.0
    hedge_failed: bool = False
    # passive (maker) hedge state, used with execution.hedge_mode = passive_first
    hedge_order: OrderState | None = None
    hedge_filled_seen: Decimal = ZERO
    hedge_started: float = 0.0
    hedge_ref_mid: Decimal | None = None
    hedge_cancel_requested: bool = False
    hedge_cancel_ts: float = 0.0
    hedge_escalate: bool = False


@dataclass
class Snapshot:
    maker_bbo: BBO
    hedge_bbo: BBO
    maker_mkt: MarketInfo
    hedge_mkt: MarketInfo
    maker_acct: AccountState
    hedge_acct: AccountState

    @property
    def maker_pos(self) -> Decimal:
        return self.maker_acct.position_size(self.maker_mkt.symbol)

    @property
    def hedge_pos(self) -> Decimal:
        return self.hedge_acct.position_size(self.hedge_mkt.symbol)


@dataclass
class PairStats:
    clips: int = 0
    maker_fills: int = 0
    hedges: int = 0
    hedge_failures: int = 0
    repairs: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    last_error: str = ""
    extra: dict = field(default_factory=dict)


class PairExecutor:
    def __init__(self, pair: PairConfig, ctx: Context, rng: random.Random | None = None):
        self.pair = pair
        self.name = pair.name
        self.ctx = ctx
        self.cfg = ctx.cfg
        self.rng = rng or random.Random()
        self.maker: Venue = ctx.venues[pair.maker.venue]
        self.hedge: Venue = ctx.venues[pair.hedge.venue]
        self.ratio = D(pair.hedge_ratio)
        self.rt = Runtime()
        self.clip: Clip | None = None
        self.stats = PairStats()
        self.settle_until = 0.0
        self.cooldown_until = 0.0
        self.imbalance_since: float | None = None
        self.basis_ref: Decimal | None = None
        self.basis_ts = 0.0
        self.disabled_reason: str | None = None

    # ------------------------------------------------------------ persistence
    def restore(self, data: dict | None) -> None:
        if data:
            known = {k: v for k, v in data.items() if k in Runtime.__dataclass_fields__}
            self.rt = Runtime(**known)

    def persist(self) -> None:
        self.ctx_save(asdict(self.rt))

    def ctx_save(self, data: dict) -> None:
        saver = getattr(self.ctx, "save_pair_state", None)
        if saver:
            saver(self.name, data)

    @property
    def busy(self) -> bool:
        return self.clip is not None

    # ------------------------------------------------------------------- tick
    def step(self, now: float | None = None) -> None:
        now = now or time.time()
        if self.disabled_reason:
            return
        try:
            if self.clip:
                self._manage_clip(now)
            elif now >= self.settle_until and now >= self.cooldown_until:
                self._decide(now)
            self.stats.consecutive_errors = 0
        except VenueError as exc:
            self.stats.errors += 1
            self.stats.consecutive_errors += 1
            self.stats.last_error = str(exc)
            level = "error" if self.stats.consecutive_errors >= self.cfg.risk.max_consecutive_errors else "warning"
            self.ctx.event(level, "venue_error", f"{self.name}: {exc}", pair=self.name, notify=level == "error",
                           throttle_key=f"err:{self.name}", throttle_s=900)

    def _snapshot(self, max_age_s: float) -> Snapshot:
        maker_mkt = self.maker.market(self.pair.maker.symbol)
        hedge_mkt = self.hedge.market(self.pair.hedge.symbol)
        maker_bbo = self.maker.bbo(self.pair.maker.symbol)
        hedge_bbo = self.hedge.bbo(self.pair.hedge.symbol)
        return Snapshot(maker_bbo, hedge_bbo, maker_mkt, hedge_mkt,
                        self.ctx.account(self.pair.maker.venue, max_age_s),
                        self.ctx.account(self.pair.hedge.venue, max_age_s))

    def _update_basis(self, snap: Snapshot, now: float) -> Decimal:
        current = strategy.basis_bps(snap.maker_bbo.mid, snap.hedge_bbo.mid)
        self.basis_ref = strategy.ewma(self.basis_ref, current, now - self.basis_ts if self.basis_ts else 0,
                                       BASIS_HALF_LIFE_S)
        self.basis_ts = now
        return current

    # --------------------------------------------------------------- decisions
    def _decide(self, now: float) -> None:
        snap = self._snapshot(self.cfg.poll.account_s)
        self._update_basis(snap, now)
        if self._handle_imbalance(snap, now):
            return
        forced = self.ctx.take_forced_reduction(self.name)
        if forced is not None and snap.maker_pos != 0:
            self._start_derisk(snap, forced, now)
            return
        desired = self._desired_position(snap, now)
        if desired is not None:
            self._maybe_start_clip(snap, desired, now)

    def _ensure_direction(self, snap: Snapshot, now: float) -> None:
        pos = snap.maker_pos
        if self.rt.direction == 0:
            if pos != 0:
                self.rt.direction = 1 if pos > 0 else -1
            else:
                self.rt.direction = strategy.choose_direction(
                    self.pair.direction,
                    self.ctx.funding_avg(self.pair.maker.venue, self.pair.maker.symbol),
                    self.ctx.funding_avg(self.pair.hedge.venue, self.pair.hedge.symbol),
                    self.cfg.strategy.min_carry_edge_apr,
                    self.ctx.maker_venue_bias(self.name, self.pair.maker.venue),
                )
            self._retarget(snap)
            self.ctx.event("info", "direction", f"{self.name}: {'long' if self.rt.direction > 0 else 'short'} maker leg",
                           pair=self.name)
            self.persist()

    def _retarget(self, snap: Snapshot) -> None:
        self.rt.target_qty = str(strategy.target_quantity(
            self.rt.direction, self.pair.notional_usd, snap.maker_bbo.mid, snap.maker_mkt.step_size))

    def _desired_position(self, snap: Snapshot, now: float) -> Decimal | None:
        s = self.cfg.strategy
        self._ensure_direction(snap, now)
        pos, target = snap.maker_pos, self.rt.target
        tol = s.target_tolerance
        phase = Phase(self.rt.phase)

        if phase is Phase.BUILD:
            if strategy.at_target(pos, target, tol):
                self.rt.phase = Phase.HOLD.value
                self.rt.next_rotation_ts = now + strategy.next_rotation_delay_s(s.min_hold_h, s.max_hold_h, self.rng)
                self.ctx.event("info", "built", f"{self.name}: at target {pos}", pair=self.name)
                self.persist()
                return None
            if now < self.rt.derisk_until and abs(target) > abs(pos):
                return None  # de-risked recently: do not add back yet
            return target

        if phase is Phase.HOLD:
            if not strategy.at_target(pos, target, tol * 2) and now >= self.rt.derisk_until:
                self.rt.phase = Phase.BUILD.value
                self._retarget(snap)
                self.persist()
                return self.rt.target
            if now < self.rt.next_rotation_ts or self.ctx.halted:
                return None
            if not strategy.rotation_window_open(s.rotate_when, snap.maker_mkt.outside_rth):
                return None
            if not self.ctx.rotation_budget_left():
                self.rt.next_rotation_ts = now + 1800
                self.ctx.event("info", "budget", f"{self.name}: daily cost budget used, rotation postponed",
                               pair=self.name, throttle_key="budget", throttle_s=3600)
                self.persist()
                return None
            flip = self.pair.direction == "auto" and strategy.should_flip(
                self.rt.direction,
                self.ctx.funding_avg(self.pair.maker.venue, self.pair.maker.symbol),
                self.ctx.funding_avg(self.pair.hedge.venue, self.pair.hedge.symbol),
                s.flip_carry_apr,
            )
            fraction = D(1) if flip else strategy.rotation_fraction(s.rotation_fraction_min, s.rotation_fraction_max, self.rng)
            unwind_to = self.maker.round_size(self.pair.maker.symbol, abs(pos) * (1 - fraction)) * (1 if pos > 0 else -1)
            self.rt.unwind_to = str(unwind_to)
            self.rt.flip_pending = flip
            self.rt.phase = Phase.UNWIND.value
            self.ctx.event("info", "rotation", f"{self.name}: rotating {fraction:.0%}{' (flip)' if flip else ''}",
                           pair=self.name)
            self.persist()
            return unwind_to

        # UNWIND
        unwind_to = D(self.rt.unwind_to)
        if abs(pos - unwind_to) <= max(abs(target) * tol, snap.maker_mkt.step_size):
            if self.rt.flip_pending:
                self.rt.direction = -self.rt.direction
                self.rt.flip_pending = False
            self._retarget(snap)
            self.rt.phase = Phase.BUILD.value
            self.rt.rotations_done += 1
            self.persist()
            return None if self.ctx.halted else self.rt.target
        return unwind_to

    # ----------------------------------------------------------------- guards
    def _guards_ok(self, snap: Snapshot, side: Side, increases: bool, now: float) -> str | None:
        """Return a reason string when a new maker clip must not start."""
        s = self.cfg.strategy
        if increases and self.ctx.halted:
            return "halted"
        if not snap.maker_mkt.online or not snap.hedge_mkt.online:
            return "market offline"
        stale = self.cfg.risk.stale_data_s
        wall = time.time()  # quote timestamps are wall-clock receive times
        if wall - snap.maker_bbo.ts > stale or wall - snap.hedge_bbo.ts > stale:
            return "stale prices"
        if snap.maker_bbo.spread_bps > s.max_maker_spread_bps:
            return f"maker spread {snap.maker_bbo.spread_bps:.1f}bps"
        if snap.hedge_bbo.spread_bps > s.max_hedge_spread_bps:
            return f"hedge spread {snap.hedge_bbo.spread_bps:.1f}bps"
        if snap.maker_mkt.is_rwa and snap.maker_mkt.venue == "arcus" and \
                minutes_to_session_boundary(now_ms()) < s.session_blackout_min:
            return "session boundary"
        maker_px = snap.maker_bbo.price_for(side, aggressive=False)
        hedge_px = snap.hedge_bbo.price_for(side.opposite(), aggressive=True)
        edge = strategy.basis_edge_bps(side, maker_px, hedge_px, self.basis_ref if self.basis_ref is not None else
                                       strategy.basis_bps(snap.maker_bbo.mid, snap.hedge_bbo.mid))
        if edge < -s.basis_guard_bps:
            return f"basis edge {edge:.1f}bps"
        bounds = snap.maker_mkt.extra
        if snap.maker_mkt.outside_rth and bounds.get("upperTradingBound") and bounds.get("lowerTradingBound"):
            if not (D(bounds["lowerTradingBound"]) < maker_px < D(bounds["upperTradingBound"])):
                return "outside off-hours price band"
        return None

    def _addable_usd(self, acct: AccountState, mkt: MarketInfo) -> Decimal:
        leverage = min(D(self.pair.leverage), mkt.max_leverage_for_new_risk())
        return strategy.addable_notional(acct.equity, acct.gross_notional, acct.free_collateral, leverage,
                                         self.cfg.risk.max_account_leverage)

    # ------------------------------------------------------------------ clips
    def _maybe_start_clip(self, snap: Snapshot, desired: Decimal, now: float) -> None:
        s = self.cfg.strategy
        pos = snap.maker_pos
        gap = desired - pos
        mid = snap.maker_bbo.mid
        min_usd = max(snap.maker_mkt.min_notional, snap.hedge_mkt.min_notional) * Decimal("1.1")
        if abs(gap) * mid < min_usd:
            return
        side = Side.for_delta(gap)
        increases = abs(pos + gap) > abs(pos)
        reason = self._guards_ok(snap, side, increases, now)
        if reason:
            self.ctx.event("info", "guard", f"{self.name}: waiting ({reason})", pair=self.name,
                           throttle_key=f"guard:{self.name}:{reason.split()[0]}", throttle_s=1800)
            return
        usd = strategy.clip_usd(abs(gap) * mid, s.clip_usd_min, s.clip_usd_max, self.rng)
        if increases:
            usd = min(usd, self._addable_usd(snap.maker_acct, snap.maker_mkt),
                      self._addable_usd(snap.hedge_acct, snap.hedge_mkt) / self.ratio)
            if usd < min_usd:
                self.ctx.event("warning", "margin", f"{self.name}: not enough free collateral to add",
                               pair=self.name, notify=True, throttle_key=f"margin:{self.name}", throttle_s=3600)
                self.cooldown_until = now + 300
                return
        qty = self.maker.round_size(self.pair.maker.symbol, usd / mid)
        if not increases:
            qty = min(qty, self.maker.round_size(self.pair.maker.symbol, abs(pos)))
        hedge_qty = self.hedge.round_size(self.pair.hedge.symbol, qty * self.ratio)
        if self.ratio == 1:
            # Size the maker clip on the coarser of the two size grids so every fill can be hedged exactly.
            qty = self.maker.round_size(self.pair.maker.symbol, min(qty, hedge_qty))
        if qty * mid < snap.maker_mkt.min_notional or hedge_qty * snap.hedge_bbo.mid < snap.hedge_mkt.min_notional:
            return
        price = snap.maker_bbo.price_for(side, aggressive=False)
        if s.maker_price == "improve":
            tick = self.maker.tick_size(self.pair.maker.symbol, price)
            if snap.maker_bbo.ask - snap.maker_bbo.bid >= 3 * tick:
                price = price + tick if side is Side.BUY else price - tick
        price = self.maker.round_price(self.pair.maker.symbol, price, "down" if side is Side.BUY else "up")
        purpose = Phase(self.rt.phase).value if Phase(self.rt.phase) is not Phase.HOLD else "build"
        order = self.maker.place_order(self.pair.maker.symbol, side, qty, price, Tif.ALO, reduce_only=not increases)
        self.ctx.journal_order(self.pair.maker.venue, self.name, purpose, order)
        if order.status is OrderStatus.REJECTED:
            self._on_reject(order, now)
            return
        self.stats.clips += 1
        self.clip = Clip(purpose, side, side.opposite(), order, mid, snap.maker_mkt.outside_rth, urgent=False)

    def _start_derisk(self, snap: Snapshot, fraction: Decimal, now: float) -> None:
        pos = snap.maker_pos
        qty = self.maker.round_size(self.pair.maker.symbol, abs(pos) * fraction)
        if qty <= 0:
            return
        side = Side.SELL if pos > 0 else Side.BUY
        bbo = snap.maker_bbo
        slip = self.cfg.execution.urgent_slippage_bps / 10000
        ref = bbo.price_for(side, aggressive=True)
        price = self.maker.round_price(self.pair.maker.symbol, ref * (1 + slip) if side is Side.BUY else ref * (1 - slip),
                                       "up" if side is Side.BUY else "down")
        order = self.maker.place_order(self.pair.maker.symbol, side, qty, price, Tif.IOC, reduce_only=True)
        self.ctx.journal_order(self.pair.maker.venue, self.name, "derisk", order)
        self.rt.derisk_until = now + 6 * 3600
        self.rt.target_qty = str(pos + side.sign * qty)
        self.rt.phase = Phase.HOLD.value
        self.rt.next_rotation_ts = now + strategy.next_rotation_delay_s(
            self.cfg.strategy.min_hold_h, self.cfg.strategy.max_hold_h, self.rng)
        self.persist()
        self.ctx.event("error", "derisk", f"{self.name}: risk reduction {fraction:.0%} ({side.value} {qty})",
                       pair=self.name, notify=True)
        if order.status is OrderStatus.REJECTED:
            self._on_reject(order, now)
            return
        self.clip = Clip("derisk", side, side.opposite(), order, bbo.mid, snap.maker_mkt.outside_rth, urgent=True)

    def _on_reject(self, order: OrderState, now: float) -> None:
        reason = order.reject_reason or "rejected"
        if "POST_ONLY" in reason.upper() or "WOULD_CROSS" in reason.upper():
            self.cooldown_until = now + 1  # price moved; requote next tick
            return
        self.cooldown_until = now + REJECT_COOLDOWN_S
        self.ctx.event("warning", "reject", f"{self.name}: {order.side.value} {order.size} rejected: {reason}",
                       pair=self.name, venue=self.maker.name, notify=True,
                       throttle_key=f"reject:{self.name}", throttle_s=1800)

    def _manage_clip(self, now: float) -> None:
        clip = self.clip
        assert clip is not None
        order = self.maker.order_state(clip.order)
        new_fill = order.filled - clip.filled_seen
        if new_fill > 0:
            fill_px = clip.order.price if clip.order.tif is Tif.ALO else (order.avg_price or clip.order.price)
            mkt = self.maker.market(self.pair.maker.symbol)
            maker = clip.order.tif is Tif.ALO
            self.ctx.journal_fill(
                venue=self.maker.name, symbol=self.pair.maker.symbol, pair=self.name, purpose=clip.purpose,
                order_id=order.order_id, side=clip.maker_side.value, size=new_fill, price=fill_px,
                fee=new_fill * fill_px * (mkt.maker_fee if maker else mkt.taker_fee),
                liquidity="maker" if maker else "taker", mid=clip.maker_mid, outside_rth=clip.outside_rth,
            )
            self.stats.maker_fills += 1
            clip.filled_seen = order.filled
            clip.hedge_pending += new_fill * self.ratio
            clip.last_fill_ts = now
            self.ctx.invalidate_account(self.pair.maker.venue)

        hedge_mkt = self.hedge.market(self.pair.hedge.symbol)
        hedge_px = hedge_mkt.mark_price if hedge_mkt.mark_price > 0 else clip.maker_mid
        if clip.hedge_order is not None:
            self._work_passive_hedge(clip, now)
        elif clip.hedge_pending > 0 and not clip.hedge_failed:
            hedge_qty = self.hedge.round_size(self.pair.hedge.symbol, clip.hedge_pending)
            if hedge_qty > 0 and hedge_qty * hedge_px >= hedge_mkt.min_notional:
                passive = self.cfg.execution.hedge_mode == "passive_first" and not clip.urgent
                if passive and not clip.hedge_escalate:
                    if not order.status.done and not clip.cancel_requested:
                        # stop adding one-legged exposure while the hedge rests on the book
                        self.maker.cancel_order(self.pair.maker.symbol, order.order_id)
                        clip.cancel_requested, clip.cancel_ts = True, now
                    self._start_passive_hedge(clip, hedge_qty, now)
                else:
                    self._hedge(clip, hedge_qty, now)
            elif order.status.done:
                # Below the hedge venue's minimum order size; the imbalance check repairs it if it ever matters.
                self.ctx.event("info", "hedge_residual", f"{self.name}: {clip.hedge_pending} left unhedged (below min size)",
                               pair=self.name)

        hedge_busy = clip.hedge_order is not None or (
            not clip.hedge_failed and clip.hedge_pending > 0
            and self.hedge.round_size(self.pair.hedge.symbol, clip.hedge_pending) * hedge_px >= hedge_mkt.min_notional)
        if order.status.done and not hedge_busy:
            self.ctx.journal_order(self.pair.maker.venue, self.name, clip.purpose + ":final", order)
            self.clip = None
            self.settle_until = now + SETTLE_S
            self.ctx.invalidate_account(self.pair.maker.venue)
            self.ctx.invalidate_account(self.pair.hedge.venue)
            if order.status is OrderStatus.REJECTED and order.filled == 0:
                self._on_reject(order, now)
            return
        if order.status.done:
            return  # maker order finished; the hedge is still being worked

        if clip.cancel_requested:
            if now - clip.cancel_ts > CANCEL_RETRY_S:
                self.maker.cancel_order(self.pair.maker.symbol, order.order_id)
                clip.cancel_ts = now
            return

        cancel_reason = self._should_cancel(clip, order, now)
        if cancel_reason:
            self.maker.cancel_order(self.pair.maker.symbol, order.order_id)
            clip.cancel_requested = True
            clip.cancel_ts = now

    def _should_cancel(self, clip: Clip, order: OrderState, now: float) -> str | None:
        s = self.cfg.strategy
        age = now - (order.created_ts or now)
        if age >= s.clip_timeout_s:
            return "timeout"
        if clip.hedge_failed:
            return "hedge failed"
        if order.tif is not Tif.ALO:
            return None
        bbo = self.maker.bbo(self.pair.maker.symbol)
        behind = bbo.bid > order.price if clip.maker_side is Side.BUY else bbo.ask < order.price
        if behind and age >= s.reprice_after_s:
            return "requote"
        if self.ctx.halted and clip.purpose == "build":
            return "halted"
        hedge_bbo = self.hedge.bbo(self.pair.hedge.symbol)
        hedge_px = hedge_bbo.price_for(clip.hedge_side, aggressive=True)
        ref = self.basis_ref if self.basis_ref is not None else strategy.basis_bps(bbo.mid, hedge_bbo.mid)
        edge = strategy.basis_edge_bps(clip.maker_side, order.price, hedge_px, ref)
        if edge < -s.basis_guard_bps * 2:
            return "basis moved"
        if hedge_bbo.spread_bps > s.max_hedge_spread_bps * 2:
            return "hedge spread"
        return None

    # ---------------------------------------------------------- passive hedge
    def _start_passive_hedge(self, clip: Clip, qty: Decimal, now: float) -> None:
        venue, symbol, side = self.hedge, self.pair.hedge.symbol, clip.hedge_side
        bbo = venue.bbo(symbol)
        price = venue.round_price(symbol, bbo.price_for(side, aggressive=False), "down" if side is Side.BUY else "up")
        order = venue.place_order(symbol, side, qty, price, Tif.ALO, reduce_only=self._hedge_reduce_only(side, qty))
        self.ctx.journal_order(venue.name, self.name, "hedge_passive", order)
        if order.status is OrderStatus.REJECTED:
            clip.hedge_escalate = True  # would cross / rejected: take liquidity on the next tick
            return
        clip.hedge_order = order
        clip.hedge_filled_seen = ZERO
        clip.hedge_cancel_requested = False
        if not clip.hedge_started:
            clip.hedge_started = now
            clip.hedge_ref_mid = bbo.mid

    def _work_passive_hedge(self, clip: Clip, now: float) -> None:
        venue, symbol, side = self.hedge, self.pair.hedge.symbol, clip.hedge_side
        cfg = self.cfg.execution
        order = venue.order_state(clip.hedge_order)
        new_fill = order.filled - clip.hedge_filled_seen
        if new_fill > 0:
            mkt = venue.market(symbol)
            self.ctx.journal_fill(
                venue=venue.name, symbol=symbol, pair=self.name, purpose="hedge", order_id=order.order_id,
                side=side.value, size=new_fill, price=order.price, fee=new_fill * order.price * mkt.maker_fee,
                liquidity="maker", mid=clip.hedge_ref_mid, outside_rth=clip.outside_rth,
                hedge_latency_ms=int((time.time() - clip.last_fill_ts) * 1000) if clip.last_fill_ts else None,
            )
            clip.hedge_pending -= new_fill
            clip.hedge_filled_seen = order.filled
            self.stats.hedges += 1
            self.ctx.invalidate_account(self.pair.hedge.venue)
        if order.status.done:
            clip.hedge_order = None
            clip.hedge_cancel_requested = False
            return
        if clip.hedge_cancel_requested:
            if now - clip.hedge_cancel_ts > CANCEL_RETRY_S:
                venue.cancel_order(symbol, order.order_id)
                clip.hedge_cancel_ts = now
            return
        bbo = venue.bbo(symbol)
        ref = clip.hedge_ref_mid or bbo.mid
        # Selling the hedge suffers when the market falls, buying it when the market rises.
        adverse_bps = (ref - bbo.mid) / ref * 10000 if side is Side.SELL else (bbo.mid - ref) / ref * 10000
        behind = bbo.ask < order.price if side is Side.SELL else bbo.bid > order.price
        if now - clip.hedge_started >= cfg.passive_hedge_timeout_s or adverse_bps >= cfg.passive_hedge_max_adverse_bps:
            clip.hedge_escalate = True
        elif not (behind and now - (order.created_ts or now) >= cfg.passive_hedge_requote_s):
            return
        venue.cancel_order(symbol, order.order_id)
        clip.hedge_cancel_requested, clip.hedge_cancel_ts = True, now

    # ------------------------------------------------------------------ hedge
    def _hedge_reduce_only(self, side: Side, qty: Decimal) -> bool:
        current = self.ctx.account(self.pair.hedge.venue, 0).position_size(self.pair.hedge.symbol)
        return current != 0 and (side.sign * current) < 0 and qty <= abs(current)

    def _wait_done(self, venue: Venue, order: OrderState) -> OrderState:
        deadline = time.time() + self.cfg.execution.ioc_confirm_timeout_s
        while not order.status.done and time.time() < deadline:
            time.sleep(0.25)
            order = venue.order_state(order)
        return order

    def _hedge(self, clip: Clip, qty: Decimal, now: float) -> None:
        venue, symbol, side = self.hedge, self.pair.hedge.symbol, clip.hedge_side
        ladder = [self.cfg.execution.urgent_slippage_bps] if clip.urgent else list(self.cfg.execution.hedge_slippage_bps)
        mkt = venue.market(symbol)
        remaining = qty
        reduce_only = self._hedge_reduce_only(side, qty)
        for slip_bps in ladder:
            bbo = venue.bbo(symbol)
            ref = bbo.price_for(side, aggressive=True)
            slip = D(slip_bps) / 10000
            raw = ref * (1 + slip) if side is Side.BUY else ref * (1 - slip)
            price = venue.round_price(symbol, raw, "up" if side is Side.BUY else "down")
            order = venue.place_order(symbol, side, remaining, price, Tif.IOC, reduce_only=reduce_only)
            order = self._wait_done(venue, order)
            self.ctx.journal_order(venue.name, self.name, "hedge", order)
            if order.filled > 0:
                fill_px = order.avg_price or price
                self.ctx.journal_fill(
                    venue=venue.name, symbol=symbol, pair=self.name, purpose="hedge", order_id=order.order_id,
                    side=side.value, size=order.filled, price=fill_px,
                    fee=order.fee if order.fee is not None else order.filled * fill_px * mkt.taker_fee,
                    liquidity="taker", mid=bbo.mid, outside_rth=clip.outside_rth,
                    hedge_latency_ms=int((time.time() - clip.last_fill_ts) * 1000) if clip.last_fill_ts else None,
                )
                remaining -= order.filled
                clip.hedge_pending -= order.filled
                self.stats.hedges += 1
                self.ctx.invalidate_account(self.pair.hedge.venue)
            elif order.status is OrderStatus.REJECTED and reduce_only and "reduce" in (order.reject_reason or "").lower():
                reduce_only = False  # our cached hedge position was stale; retry as a plain order
            if remaining * ref < mkt.min_notional:
                return
        # Ladder exhausted with a meaningful unhedged quantity: close it on the maker leg.
        unhedged_maker = remaining / self.ratio
        if unhedged_maker * clip.maker_mid > self.cfg.execution.imbalance_tolerance_usd:
            clip.hedge_failed = True
            self.stats.hedge_failures += 1
            self.ctx.event("critical", "hedge_failed",
                           f"{self.name}: could not hedge {remaining} on {venue.name}; closing maker leg instead",
                           pair=self.name, venue=venue.name, notify=True)
            self.flatten_leg(self.maker, self.pair.maker.symbol, clip.maker_side.opposite(), unhedged_maker,
                              "hedge_fail_unwind", clip.outside_rth)
            clip.hedge_pending = ZERO
            self.cooldown_until = now + HEDGE_FAILURE_COOLDOWN_S

    def flatten_leg(self, venue: Venue, symbol: str, side: Side, qty: Decimal, purpose: str, outside_rth: bool) -> Decimal:
        """Reduce-only IOC on one leg (imbalance repair / failed hedge). Returns filled qty."""
        qty = venue.round_size(symbol, qty)
        if qty <= 0:
            return ZERO
        mkt = venue.market(symbol)
        bbo = venue.bbo(symbol)
        slip = self.cfg.execution.urgent_slippage_bps / 10000
        ref = bbo.price_for(side, aggressive=True)
        price = venue.round_price(symbol, ref * (1 + slip) if side is Side.BUY else ref * (1 - slip),
                                  "up" if side is Side.BUY else "down")
        order = venue.place_order(symbol, side, qty, price, Tif.IOC, reduce_only=True)
        order = self._wait_done(venue, order)
        self.ctx.journal_order(venue.name, self.name, purpose, order)
        if order.filled > 0:
            fill_px = order.avg_price or price
            self.ctx.journal_fill(
                venue=venue.name, symbol=symbol, pair=self.name, purpose=purpose, order_id=order.order_id,
                side=side.value, size=order.filled, price=fill_px,
                fee=order.fee if order.fee is not None else order.filled * fill_px * mkt.taker_fee,
                liquidity="taker", mid=bbo.mid, outside_rth=outside_rth,
            )
        self.ctx.invalidate_account(venue.name)
        return order.filled

    # -------------------------------------------------------------- imbalance
    def leg_imbalance(self, snap: Snapshot) -> Decimal:
        """Net exposure in maker units (0 when perfectly hedged)."""
        return snap.maker_pos + snap.hedge_pos / self.ratio

    def _handle_imbalance(self, snap: Snapshot, now: float) -> bool:
        net = self.leg_imbalance(snap)
        net_usd = abs(net) * snap.maker_bbo.mid
        if net_usd <= self.cfg.execution.imbalance_tolerance_usd:
            self.imbalance_since = None
            return False
        if self.imbalance_since is None:
            self.imbalance_since = now
            self.ctx.invalidate_account(self.pair.maker.venue)
            self.ctx.invalidate_account(self.pair.hedge.venue)
            return True
        if now - self.imbalance_since < self.cfg.execution.imbalance_grace_s:
            self.ctx.invalidate_account(self.pair.maker.venue)
            self.ctx.invalidate_account(self.pair.hedge.venue)
            return True
        maker_abs, hedge_abs = abs(snap.maker_pos), abs(snap.hedge_pos / self.ratio)
        if maker_abs >= hedge_abs:
            venue, symbol, pos, qty = self.maker, self.pair.maker.symbol, snap.maker_pos, min(abs(net), maker_abs)
        else:
            venue, symbol, pos = self.hedge, self.pair.hedge.symbol, snap.hedge_pos
            qty = min(abs(net) * self.ratio, abs(pos))
        side = Side.SELL if pos > 0 else Side.BUY
        leg_lost = snap.maker_pos == 0 or snap.hedge_pos == 0
        self.ctx.event("warning", "imbalance",
                       f"{self.name}: legs off by {net} (${net_usd:.0f}); {side.value} {qty} on {venue.name}",
                       pair=self.name, venue=venue.name, notify=True)
        self.stats.repairs += 1
        self.flatten_leg(venue, symbol, side, qty, "repair", snap.maker_mkt.outside_rth)
        self.imbalance_since = None
        self.settle_until = now + SETTLE_S
        if leg_lost:
            self._after_external_close(now)
        return True

    def _after_external_close(self, now: float) -> None:
        """A whole leg vanished (exchange stop, liquidation, or a hedge interrupted by a crash).

        The other leg has just been closed by the repair. Stay flat for the cool-off, then rebuild
        from scratch (direction re-decided) only if both venues have the margin for it.
        """
        cool_h = self.cfg.risk.stop_cooldown_h
        self.rt.direction = 0
        self.rt.phase = Phase.BUILD.value
        self.rt.target_qty = "0"
        self.rt.flip_pending = False
        self.rt.derisk_until = now + (cool_h * 3600 if cool_h > 0 else 10 * 365 * 86400)
        self.persist()
        wait = f"rebuilds after {cool_h:g}h if margin allows" if cool_h > 0 else "stays flat until you restart it"
        self.ctx.event("critical", "leg_lost",
                       f"{self.name}: one leg was closed outside the bot (exchange stop / liquidation / interrupted "
                       f"hedge). Closed the other leg too; the pair {wait}. Check venue balances.",
                       pair=self.name, notify=True)

    # --------------------------------------------------------------- shutdown
    def shutdown(self, timeout_s: float = 20) -> None:
        """Cancel the resting maker order and hedge whatever filled before the cancel landed."""
        if not self.clip:
            return
        try:
            clip = self.clip
            if not clip.order.status.done:
                self.maker.cancel_order(self.pair.maker.symbol, clip.order.order_id)
            clip.cancel_requested = True
            clip.cancel_ts = time.time()
            clip.hedge_escalate = True  # no time to wait for a passive hedge: cross the spread
            if clip.hedge_order is not None and not clip.hedge_cancel_requested:
                self.hedge.cancel_order(self.pair.hedge.symbol, clip.hedge_order.order_id)
                clip.hedge_cancel_requested, clip.hedge_cancel_ts = True, time.time()
            deadline = time.time() + timeout_s
            while self.clip and time.time() < deadline:
                self._manage_clip(time.time())
                if self.clip:
                    time.sleep(0.5)
        except VenueError as exc:
            self.ctx.event("error", "shutdown", f"{self.name}: shutdown cancel failed: {exc}", pair=self.name,
                           notify=True)

    def status(self) -> dict:
        return {
            "pair": self.name,
            "phase": self.rt.phase,
            "direction": self.rt.direction,
            "target": self.rt.target_qty,
            "next_rotation_in_min": round(max(0.0, self.rt.next_rotation_ts - time.time()) / 60, 1)
            if self.rt.phase == Phase.HOLD.value else None,
            "rotations": self.rt.rotations_done,
            "busy": self.busy,
            "stats": asdict(self.stats),
            "disabled": self.disabled_reason,
        }

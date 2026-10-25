"""Main loop: wires venues, executors, risk manager, journal and notifier together."""

from __future__ import annotations

import logging
import random
import signal
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .backstop import BackstopManager
from .config import Config, Secrets
from .executor import PairExecutor
from .journal import Journal
from .models import AccountState, OrderState, Side
from .notifier import Notifier
from .rebalance import describe, plan_transfers, weights_from
from .risk import RiskManager
from .sizing import SizeScaler
from .util import ZERO, D
from .venues.arcus import ArcusSigner, ArcusVenue
from .venues.base import Venue, VenueError
from .venues.hyperliquid import STANDARD_MODES, HyperliquidClient, HyperliquidVenue
from .venues.paper import PaperVenue

log = logging.getLogger(__name__)

HL_MODE_HELP = ("Hyperliquid account {address} is in '{mode}' mode. The bot needs Standard mode "
                "(separate trade.xyz / Entropy balances): switch it in the Hyperliquid app or run "
                "`hedgebot hl-mode --set-standard --i-understand-live`, then move USDC into the xyz and io dexes.")


def check_hl_mode(venues: dict[str, Venue]) -> None:
    """Refuse to trade when the HL account pools balances across dexes (per-dex states are meaningless then)."""
    clients = {id(v.client): v.client for v in venues.values() if isinstance(v, HyperliquidVenue) and v.can_trade}
    for client in clients.values():
        mode = client.account_mode()
        if mode not in STANDARD_MODES:
            raise SystemExit(HL_MODE_HELP.format(address=client.account_address, mode=mode))


def build_market_venues(cfg: Config, secrets: Secrets | None, *, trading: bool) -> dict[str, Venue]:
    """Real venues; with trading=False they are public (market data only)."""
    names = cfg.venues_needed()
    venues: dict[str, Venue] = {}
    network = cfg.network
    if "arcus" in names:
        signer = None
        if trading:
            if not (secrets and secrets.arcus_privkey and secrets.arcus_address):
                raise SystemExit("ARCUS_API_PRIVKEY / ARCUS_ETH_ADDRESS are required for testnet/live mode (.env)")
            signer = ArcusSigner(secrets.arcus_privkey, secrets.arcus_address, cfg.arcus.account_index)
        venues["arcus"] = ArcusVenue(network, signer)
    hl_names = [n for n in names if n.startswith("hl:")]
    if hl_names:
        account = agent = None
        if trading:
            if not (secrets and secrets.hl_account and secrets.hl_agent_key):
                raise SystemExit("HL_ACCOUNT_ADDRESS / HL_AGENT_PRIVKEY are required for testnet/live mode (.env)")
            account, agent = secrets.hl_account, secrets.hl_agent_key
        client = HyperliquidClient(network, account, agent, dexes=[n[3:] for n in hl_names])
        for n in hl_names:
            venues[n] = HyperliquidVenue(client, n[3:], base_maker_fee=cfg.hyperliquid.base_maker_fee,
                                         base_taker_fee=cfg.hyperliquid.base_taker_fee)
    return venues


class Engine:
    def __init__(self, cfg: Config, secrets: Secrets, *, journal: Journal | None = None):
        self.cfg = cfg
        self.secrets = secrets
        self.journal = journal or Journal(cfg.journal_path)
        self.notifier = Notifier(secrets.telegram_token, secrets.telegram_chat, prefix=f"[hedgebot {cfg.mode}] ")
        self.halted = False
        self.halt_reason: str | None = None
        self._stop = False
        self._accounts: dict[str, AccountState] = {}
        self._account_ts: dict[str, float] = {}
        self._forced: dict[str, Decimal] = {}
        self._funding_cache: dict[tuple[str, str], tuple[float, Decimal]] = {}
        self._timers: dict[str, float] = {}
        self.rng = random.Random(cfg.strategy.seed)
        saved = self.journal.load_pair_state("__sizing__") or {}
        self.scaler = SizeScaler(cfg.sizing, D(saved.get("scale", 1)), int(saved.get("version", 0)), bool(saved))
        if not cfg.sizing.scale_with_equity and self.scaler.scale != 1:
            # sizing switched off: back to the configured notional (the version bump retargets every pair)
            self.scaler.scale, self.scaler.version = D(1), self.scaler.version + 1
            self.journal.save_pair_state("__sizing__", self.scaler.state())

        trading = cfg.mode in ("testnet", "live")
        market_venues = build_market_venues(cfg, secrets, trading=trading)
        if cfg.mode == "paper":
            self.venues: dict[str, Venue] = {
                name: PaperVenue(v, D(cfg.capital.get(name, 0)), state_dir=cfg.paper.state_dir,
                                 maker_fill=cfg.paper.maker_fill)
                for name, v in market_venues.items()
            }
            for name, v in self.venues.items():
                if D(cfg.capital.get(name, 0)) <= 0 and not getattr(v, "positions", None):
                    log.warning("paper venue %s starts with zero balance (set capital.%s)", name, name)
        else:
            self.venues = market_venues
        self.risk = RiskManager(cfg, self)
        self.executors: list[PairExecutor] = []
        self.backstops = BackstopManager(self) if cfg.risk.backstop_enabled else None

    # ------------------------------------------------------------ context API
    def account(self, venue: str, max_age_s: float = 5.0) -> AccountState:
        ts = self._account_ts.get(venue, 0)
        if venue not in self._accounts or time.time() - ts > max_age_s:
            self._accounts[venue] = self.venues[venue].account()
            self._account_ts[venue] = time.time()
        return self._accounts[venue]

    def invalidate_account(self, venue: str) -> None:
        self._account_ts[venue] = 0

    def request_reduction(self, pair: str, fraction: Decimal, reason: str) -> None:
        self._forced[pair] = max(fraction, self._forced.get(pair, ZERO))
        self.event("error", "derisk_request", f"{pair}: cut {fraction:.0%} ({reason})", pair=pair)

    def take_forced_reduction(self, pair: str) -> Decimal | None:
        return self._forced.pop(pair, None)

    def has_forced_reduction(self, pair: str) -> bool:
        return pair in self._forced

    def rotation_budget_left(self) -> bool:
        day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return self.journal.cost_since(day_start) < self.cfg.strategy.max_daily_cost_usd * self.scaler.scale

    def size_scale(self) -> tuple[Decimal, int]:
        return self.scaler.scale, self.scaler.version

    def funding_avg(self, venue: str, symbol: str) -> Decimal:
        key = (venue, symbol)
        cached = self._funding_cache.get(key)
        if cached and time.time() - cached[0] < self.cfg.poll.funding_refresh_s:
            return cached[1]
        v = self.venues[venue]
        try:
            end = int(time.time() * 1000)
            rows = v.funding_history(symbol, end - int(self.cfg.strategy.funding_window_h * 3_600_000), end)
            value = sum((r for _, r in rows), ZERO) / len(rows) if rows else v.market(symbol).funding_rate_1h
        except VenueError as exc:
            log.warning("funding history %s %s failed: %s", venue, symbol, exc)
            value = v.market(symbol).funding_rate_1h
        self._funding_cache[key] = (time.time(), value)
        return value

    def maker_venue_bias(self, pair: str, venue: str) -> Decimal:
        bias = ZERO
        for ex in self.executors:
            if ex.name != pair and ex.pair.maker.venue == venue and ex.rt.direction:
                bias += ex.rt.direction * ex.pair.notional_usd
        return bias

    def journal_fill(self, **kwargs) -> None:
        self.journal.fill(**kwargs)

    def journal_order(self, venue: str, pair: str, purpose: str, order: OrderState) -> None:
        self.journal.order(venue, pair, purpose, order)

    def save_pair_state(self, pair: str, data: dict) -> None:
        self.journal.save_pair_state(pair, data)

    def event(self, level: str, kind: str, message: str, *, pair=None, venue=None, data=None, notify=False,
              throttle_key=None, throttle_s=0.0) -> None:
        if throttle_key and throttle_s:
            last = self._timers.get(f"evt:{throttle_key}", 0)
            if time.time() - last < throttle_s:
                return
            self._timers[f"evt:{throttle_key}"] = time.time()
        self.journal.event(level, kind, message, pair=pair, venue=venue, data=data)
        if notify:
            self.notifier.send(message, level=level)
        else:
            getattr(log, "error" if level == "critical" else level if level in ("info", "warning", "error") else "info")(
                message)

    def halt(self, reason: str, detail: str) -> None:
        self.halted, self.halt_reason = True, reason
        self.event("critical", "halt", f"HALT ({reason}): {detail}. No new risk; hedged positions are kept.",
                   notify=True)

    def resume(self, why: str) -> None:
        self.halted, self.halt_reason = False, None
        self.event("info", "resume", f"resumed ({why})", notify=True)

    # ----------------------------------------------------------------- sizing
    def measure_scale(self) -> tuple[Decimal, Decimal, str] | None:
        """(total equity / total capital, lowest per-venue ratio, that venue); None when unmeasurable."""
        venues = [v for v in self.venues if D(self.cfg.capital.get(v, 0)) > 0]
        base = sum((D(self.cfg.capital[v]) for v in venues), ZERO)
        if base <= 0:
            return None
        try:
            equity = {v: self.account(v, self.cfg.poll.account_s).equity for v in venues}
        except VenueError as exc:
            log.warning("sizing: equity read failed: %s", exc)
            return None
        ratios = {v: equity[v] / D(self.cfg.capital[v]) for v in venues}
        tightest = min(ratios, key=ratios.get)
        return sum(equity.values(), ZERO) / base, ratios[tightest], tightest

    def _update_scale(self, now: float | None = None) -> None:
        z = self.cfg.sizing
        if not z.scale_with_equity:
            return
        measured = self.measure_scale()
        if measured is None:
            return
        total, tightest, short_venue = measured
        raw = self.scaler.target(total, tightest)
        if total > self.scaler.scale * (1 + z.min_change_pct / 100) and raw < total:
            self.event("info", "sizing", f"equity is x{total:.2f} of the configured capital, but {short_venue} only "
                       f"x{tightest:.2f}: move money there to grow every pair (hedgebot rebalance)", notify=True,
                       throttle_key="sizing_short", throttle_s=6 * 3600)
        first = not self.scaler.known
        old = self.scaler.scale
        result = self.scaler.observe(raw, now or time.time())
        if first or (result and result[0] == "apply"):
            self.journal.save_pair_state("__sizing__", self.scaler.state())
        if not result:
            return
        kind, value = result
        if kind == "pending":
            wait = f"{z.grow_after_min:g} min" if value > old else f"{z.shrink_after_h:g} h"
            self.event("info", "sizing", f"equity is x{value:.2f} of the configured capital (positions sized for "
                       f"x{old}); resizing if it holds for {wait}", notify=True, throttle_key="sizing_pending",
                       throttle_s=3600)
        elif kind == "cleared":
            log.info("sizing: equity back near x%s, no resize", old)
        else:
            total = sum((p.notional_usd for p in self.cfg.pairs if p.enabled), ZERO) * value
            self.event("warning", "sizing", f"position size x{old} -> x{value}; total pair notional now "
                       f"${total:,.0f}", notify=True)

    # ----------------------------------------------------------------- set-up
    def _due(self, key: str, every_s: float) -> bool:
        if time.time() - self._timers.get(key, 0) >= every_s:
            self._timers[key] = time.time()
            return True
        return False

    def setup(self, cancel_orders: bool = True) -> None:
        for name, venue in self.venues.items():
            venue.refresh_markets()
            if isinstance(venue, ArcusVenue):
                venue.sync_clock()
        if self.cfg.mode in ("testnet", "live"):
            check_hl_mode(self.venues)
        for pair in self.cfg.pairs:
            if not pair.enabled:
                continue
            missing = [leg for leg in (pair.maker, pair.hedge) if leg.symbol not in self._markets(leg.venue)]
            if missing:
                self.event("error", "setup", f"{pair.name}: market(s) not found: "
                           f"{', '.join(f'{m.venue}/{m.symbol}' for m in missing)} - pair disabled", notify=True)
                continue
            ex = PairExecutor(pair, self, self.rng)
            ex.restore(self.journal.load_pair_state(pair.name))
            self.executors.append(ex)
        if not self.executors:
            raise SystemExit("no tradeable pairs")
        self._update_scale()
        self._configure_margin()  # paper venues simulate the same leverage / margin mode
        if self.cfg.mode in ("testnet", "live") and cancel_orders:
            for name, venue in self.venues.items():
                try:
                    venue.cancel_all()
                except VenueError as exc:
                    self.event("warning", "setup", f"{name}: cancel_all failed: {exc}")
        for name in self.venues:
            try:  # after cancel_all: one slow venue must not hold the stops of the others hostage
                acct = self.account(name, 0)
            except VenueError as exc:
                self.event("warning", "setup", f"{name}: account read failed at start-up: {exc}", notify=True)
                continue
            self.journal.snapshot(acct)
            self.event("info", "setup", f"{name}: equity ${acct.equity:,.2f}, positions "
                       f"{ {s: str(p.size) for s, p in acct.positions.items()} }")
        self.event("info", "start", f"started in {self.cfg.mode} mode with pairs "
                   f"{[e.name for e in self.executors]}", notify=True)

    def _markets(self, venue: str) -> dict:
        v = self.venues[venue]
        inner = getattr(v, "data", v)
        return getattr(inner, "_markets", {}) or inner.refresh_markets()

    def _configure_margin(self) -> None:
        for ex in self.executors:
            for leg, mode in ((ex.pair.maker, self.cfg.arcus.margin_mode if ex.pair.maker.venue == "arcus"
                               else self.cfg.hyperliquid.margin_mode),
                              (ex.pair.hedge, self.cfg.arcus.margin_mode if ex.pair.hedge.venue == "arcus"
                               else self.cfg.hyperliquid.margin_mode)):
                venue = self.venues[leg.venue]
                market = venue.market(leg.symbol)
                lev = int(min(D(ex.pair.leverage), market.max_leverage))
                if market.isolated_only:
                    mode = "isolated"
                try:
                    venue.set_leverage(leg.symbol, lev, cross=(mode == "cross"))
                    self.event("info", "setup", f"{leg.venue} {leg.symbol}: leverage {lev}x {mode}")
                except VenueError as exc:
                    self.event("warning", "setup", f"{leg.venue} {leg.symbol}: set leverage failed: {exc}",
                               notify=True)

    # ------------------------------------------------------------------- loop
    def _install_signals(self) -> None:
        def handler(signum, _frame):
            log.info("signal %s received, shutting down", signum)
            self._stop = True

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    def _heartbeat(self) -> None:
        for name, venue in self.venues.items():
            if isinstance(venue, ArcusVenue) and venue.can_trade:
                try:
                    venue.heartbeat(self.cfg.arcus.dead_mans_switch_s)
                except VenueError as exc:
                    self.event("warning", "dms", f"{name}: dead man's switch refresh failed: {exc}",
                               notify=True, throttle_key="dms", throttle_s=3600)

    def _reconcile_fills(self) -> None:
        since = int((time.time() - 3 * 3600) * 1000)
        for name, venue in self.venues.items():
            try:
                self.journal.venue_fills(venue.recent_fills(since))
            except VenueError as exc:
                log.warning("fill reconciliation %s failed: %s", name, exc)

    def _log_status(self) -> None:
        parts = []
        for name in self.venues:
            acct = self._accounts.get(name)
            if acct:
                ratio = acct.margin_ratio
                parts.append(f"{name}: eq ${acct.equity:,.0f} notional ${acct.gross_notional:,.0f}"
                             f"{f' mr {ratio:.1f}' if ratio else ''}")
        for ex in self.executors:
            st = ex.status()
            parts.append(f"{ex.name}: {st['phase']}{'*' if st['busy'] else ''} dir {st['direction']:+d} "
                         f"rot {st['rotations']}")
        if self.backstops:
            parts.append(f"stops {len(self.backstops.active)}")
        parts.append(f"size x{self.scaler.scale}")
        log.info("status | %s%s", " | ".join(parts), " | HALTED" if self.halted else "")

    def _guarded(self, name: str, fn) -> None:
        """Run one housekeeping step; an error in it must not stop risk checks, stops or the other pairs."""
        try:
            fn()
        except VenueError as exc:
            self.event("warning", name, f"{name} failed: {exc}", throttle_key=f"tickerr:{name}", throttle_s=600)
        except Exception as exc:
            log.exception("%s failed", name)
            self.event("error", "bug", f"{name} failed unexpectedly: {exc!r}", notify=True,
                       throttle_key=f"tickbug:{name}", throttle_s=900)

    def _refresh_markets(self) -> None:
        for name, venue in self.venues.items():
            try:
                venue.refresh_markets()
                if isinstance(venue, ArcusVenue) and self._due("clock", 600):
                    venue.sync_clock()
            except VenueError as exc:
                log.warning("market refresh %s failed: %s", name, exc)

    def _snapshots(self) -> None:
        for name in self.venues:
            try:
                self.journal.snapshot(self.account(name, self.cfg.poll.account_s))
            except VenueError as exc:
                log.warning("snapshot %s failed: %s", name, exc)
        self._log_status()

    def tick(self) -> None:
        cfg = self.cfg
        if self._due("markets", cfg.poll.markets_s):
            self._guarded("markets", self._refresh_markets)
        dms = cfg.arcus.dead_mans_switch_s
        if self.cfg.mode != "paper" and dms and self._due("heartbeat", max(2, dms / 4)):
            self._guarded("heartbeat", self._heartbeat)
        self._guarded("risk", self.risk.evaluate)
        for ex in self.executors:
            ex.step()  # contains its own errors
        if self.backstops and self._due("backstop", min(10, cfg.risk.backstop_refresh_s) if self.backstops.skipped
                                        else cfg.risk.backstop_refresh_s):
            self._guarded("backstop", self.backstops.refresh)
        if self._due("snapshot", cfg.poll.snapshot_s):
            self._guarded("snapshot", self._snapshots)
        if self._due("reconcile", 600):
            self._guarded("reconcile", self._reconcile_fills)
        if self.cfg.rebalance.enabled and self._due("rebalance_check", 300):
            self._guarded("rebalance", self._rebalance_reminder)
        if self._due("sizing", cfg.sizing.check_min * 60):
            self._guarded("sizing", self._update_scale)

    def rebalance_plan(self) -> tuple[list, dict, dict]:
        rb = self.cfg.rebalance
        equity = {v: self.account(v, 0).equity for v in rb.venues if v in self.venues}
        weights = {v: w for v, w in weights_from(self.cfg.capital, rb.venues).items() if v in equity}
        return plan_transfers(equity, weights, rb.min_transfer_usd), equity, weights

    def _rebalance_reminder(self) -> None:
        state = self.journal.load_pair_state("__rebalance__") or {}
        if time.time() - float(state.get("last_ts", 0)) < self.cfg.rebalance.every_h * 3600:
            return
        try:
            transfers, equity, weights = self.rebalance_plan()
        except VenueError as exc:
            log.warning("rebalance plan failed: %s", exc)
            return
        self.event("warning" if transfers else "info", "rebalance", describe(transfers, equity, weights),
                   notify=True)
        self.journal.save_pair_state("__rebalance__", {"last_ts": time.time()})

    def _setup_with_retry(self) -> None:
        """A venue outage at start-up is waited out (with alerts) instead of exiting into a restart loop."""
        delay = 15.0
        while True:
            try:
                self.setup()
                return
            except VenueError as exc:
                self.executors.clear()
                self.event("error", "setup", f"start-up failed ({exc}); retrying in {delay:.0f}s", notify=True,
                           throttle_key="setup", throttle_s=1800)
            until = time.time() + delay
            while time.time() < until:
                if self._stop:
                    raise SystemExit("stopped during start-up")
                time.sleep(1)
            delay = min(delay * 2, 300)

    def run(self, max_runtime_s: float | None = None) -> None:
        self._install_signals()
        try:
            self._setup_with_retry()
        except BaseException as exc:
            if not (isinstance(exc, SystemExit) and str(exc) == "stopped during start-up"):
                self.notifier.send(f"start-up aborted, the bot is NOT running: {exc}", level="critical")
            self.notifier.flush()
            raise
        if self.backstops:  # start-up canceled the old stops: put them back before anything else
            self._guarded("backstop", self.backstops.refresh)
            self._timers["backstop"] = time.time()
        started = time.time()
        try:
            while not self._stop:
                loop_start = time.time()
                self.tick()
                if max_runtime_s and time.time() - started >= max_runtime_s:
                    break
                interval = self.cfg.poll.active_s if any(ex.busy for ex in self.executors) else self.cfg.poll.idle_s
                time.sleep(max(0.0, interval - (time.time() - loop_start)))
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        for ex in self.executors:
            try:
                ex.shutdown()
            except Exception:
                log.exception("%s: shutdown failed", ex.name)
        left = [ex.name for ex in self.executors if ex.clip is not None]
        for name, venue in self.venues.items():
            if isinstance(venue, ArcusVenue) and venue.can_trade and self.cfg.arcus.dead_mans_switch_s and not left:
                try:  # with an order still resting, leave the switch armed so the venue cancels it
                    venue.disarm_heartbeat()
                except VenueError as exc:
                    log.warning("%s: disarm dead man's switch failed: %s", name, exc)
        for name in self.venues:
            try:
                self.journal.snapshot(self.account(name, 0))
            except Exception:
                pass
        if left:
            self.event("critical", "stop", f"stopped, but an order of {', '.join(left)} may still rest on the book: "
                       "check open orders. Positions and exchange stops are left in place.", notify=True)
        else:
            self.event("info", "stop", "stopped (positions and exchange stops left in place, resting orders "
                       "canceled)", notify=True)
        self.notifier.flush()


def flatten_all(engine: Engine, attempts: int = 3) -> list[str]:
    """Emergency: close both legs of every pair with reduce-only IOCs. Returns what could not be closed.

    The hedge leg is only reduced to mirror what is left of the maker leg, so a maker close that
    fills partly never leaves the maker leg naked. Whatever is left keeps an exchange stop.
    """
    if not engine.executors:
        engine.setup(cancel_orders=False)  # keep the exchange stops until each leg is actually closed
    if engine.cfg.mode in ("testnet", "live"):
        for name, venue in engine.venues.items():
            try:  # a resting maker / passive hedge left by a crash would reopen a leg after the flatten
                venue.cancel_resting()
            except VenueError as exc:
                engine.event("critical", "flatten", f"{name}: could not cancel resting orders: {exc}", notify=True)
    leftovers = []
    try:
        for ex in engine.executors:
            leftovers += _flatten_pair(engine, ex, attempts)
    finally:
        if engine.backstops:
            try:
                engine.backstops.refresh()  # stops for whatever is left; flat legs get none
            except Exception:
                log.exception("stop refresh after flatten failed")
        if leftovers:
            engine.event("critical", "flatten", "still open after flatten: " + "; ".join(leftovers), notify=True)
        engine.notifier.flush()
    return leftovers


def _flatten_pair(engine: Engine, ex, attempts: int) -> list[str]:
    maker_sym, hedge_sym = ex.pair.maker.symbol, ex.pair.hedge.symbol
    try:
        for _ in range(attempts):
            m = engine.account(ex.maker.name, 0).position_size(maker_sym)
            if m != 0:
                filled = ex.flatten_leg(ex.maker, maker_sym, Side.SELL if m > 0 else Side.BUY, abs(m), "flatten", False)
                engine.event("warning", "flatten", f"{ex.maker.name} {maker_sym}: closed {filled} of {m}", notify=True)
                m = engine.account(ex.maker.name, 0).position_size(maker_sym)
            h = engine.account(ex.hedge.name, 0).position_size(hedge_sym)
            excess = h + m * ex.ratio  # hedge beyond what still mirrors the maker leg
            if ex.hedge.round_size(hedge_sym, abs(excess)) > 0 and (h > 0) == (excess > 0):
                qty = min(abs(excess), abs(h))
                filled = ex.flatten_leg(ex.hedge, hedge_sym, Side.SELL if h > 0 else Side.BUY, qty, "flatten", False)
                engine.event("warning", "flatten", f"{ex.hedge.name} {hedge_sym}: closed {filled} of {h}", notify=True)
            if m == 0 and engine.account(ex.hedge.name, 0).position_size(hedge_sym) == 0:
                break
        m = engine.account(ex.maker.name, 0).position_size(maker_sym)
        h = engine.account(ex.hedge.name, 0).position_size(hedge_sym)
    except VenueError as exc:
        engine.event("critical", "flatten", f"{ex.name}: flatten failed: {exc}", pair=ex.name, notify=True)
        return [f"{ex.name}: unknown ({exc})"]
    ex.rt.direction = 0
    ex.rt.phase = "build"
    ex.rt.target_qty = "0"
    ex.persist()
    return [f"{ex.name}: {ex.maker.name} {m}, {ex.hedge.name} {h}"] if m or h else []


def acquire_run_lock(cfg: Config):
    """Exclusive lock next to the journal: one running bot per config. Returns the open lock file or None."""
    import fcntl

    path = Path(cfg.journal_path).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def ensure_dirs(cfg: Config) -> None:
    for p in (Path(cfg.journal_path).parent, Path(cfg.log_dir), Path(cfg.paper.state_dir)):
        p.mkdir(parents=True, exist_ok=True)

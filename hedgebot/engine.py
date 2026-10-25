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
from .risk import RiskManager
from .util import ZERO, D
from .venues.arcus import ArcusSigner, ArcusVenue
from .venues.base import Venue, VenueError
from .venues.hyperliquid import HyperliquidClient, HyperliquidVenue
from .venues.paper import PaperVenue

log = logging.getLogger(__name__)


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

    def rotation_budget_left(self) -> bool:
        day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return self.journal.cost_since(day_start) < self.cfg.strategy.max_daily_cost_usd

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

    # ----------------------------------------------------------------- set-up
    def _due(self, key: str, every_s: float) -> bool:
        if time.time() - self._timers.get(key, 0) >= every_s:
            self._timers[key] = time.time()
            return True
        return False

    def setup(self) -> None:
        for name, venue in self.venues.items():
            venue.refresh_markets()
            if isinstance(venue, ArcusVenue):
                venue.sync_clock()
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
        self._configure_margin()  # paper venues simulate the same leverage / margin mode
        if self.cfg.mode in ("testnet", "live"):
            for name, venue in self.venues.items():
                try:
                    venue.cancel_all()
                except VenueError as exc:
                    self.event("warning", "setup", f"{name}: cancel_all failed: {exc}")
        for name in self.venues:
            acct = self.account(name, 0)
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
        log.info("status | %s%s", " | ".join(parts), " | HALTED" if self.halted else "")

    def tick(self) -> None:
        cfg = self.cfg
        if self._due("markets", cfg.poll.markets_s):
            for name, venue in self.venues.items():
                try:
                    venue.refresh_markets()
                except VenueError as exc:
                    log.warning("market refresh %s failed: %s", name, exc)
        if self.cfg.mode != "paper" and self._due("heartbeat", max(5, cfg.arcus.dead_mans_switch_s / 4)):
            self._heartbeat()
        try:
            self.risk.evaluate()
        except VenueError as exc:
            self.event("warning", "risk", f"risk check failed: {exc}", throttle_key="riskerr", throttle_s=600)
        for ex in self.executors:
            ex.step()
        if self.backstops and self._due("backstop", cfg.risk.backstop_refresh_s):
            self.backstops.refresh()
        if self._due("snapshot", cfg.poll.snapshot_s):
            for name in self.venues:
                try:
                    self.journal.snapshot(self.account(name, cfg.poll.account_s))
                except VenueError as exc:
                    log.warning("snapshot %s failed: %s", name, exc)
            self._log_status()
        if self._due("reconcile", 600):
            self._reconcile_fills()

    def run(self, max_runtime_s: float | None = None) -> None:
        self._install_signals()
        self.setup()
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
            ex.shutdown()
        for name, venue in self.venues.items():
            if isinstance(venue, ArcusVenue) and venue.can_trade:
                try:
                    venue.disarm_heartbeat()
                except VenueError as exc:
                    log.warning("%s: disarm dead man's switch failed: %s", name, exc)
        for name in self.venues:
            try:
                self.journal.snapshot(self.account(name, 0))
            except VenueError:
                pass
        self.event("info", "stop", "stopped (positions left in place, resting orders canceled)", notify=True)


def flatten_all(engine: Engine) -> None:
    """Emergency: close both legs of every pair with reduce-only IOCs."""
    if not engine.executors:
        engine.setup()
    for ex in engine.executors:
        for venue, symbol in ((ex.maker, ex.pair.maker.symbol), (ex.hedge, ex.pair.hedge.symbol)):
            pos = engine.account(venue.name, 0).position_size(symbol)
            if pos != 0:
                filled = ex.flatten_leg(venue, symbol, Side.SELL if pos > 0 else Side.BUY, abs(pos), "flatten", False)
                engine.event("warning", "flatten", f"{venue.name} {symbol}: closed {filled} of {pos}", notify=True)
        ex.rt.direction = 0
        ex.rt.phase = "build"
        ex.rt.target_qty = "0"
        ex.persist()
    if engine.backstops:
        engine.backstops.cancel_all()


def ensure_dirs(cfg: Config) -> None:
    for p in (Path(cfg.journal_path).parent, Path(cfg.log_dir), Path(cfg.paper.state_dir)):
        p.mkdir(parents=True, exist_ok=True)

"""Account-level risk checks that sit above the per-pair executors.

- Cross accounts: margin ratio = equity / maintenance requirement (liquidation at 1.0).
  Below `warn` it alerts with the transfer that would restore it; below `critical`
  it orders every pair on that venue to cut `derisk_fraction` with IOCs on both legs.
- Isolated positions (Entropy is isolated-only): distance from mark to the
  liquidation price, same warn / critical ladder.
- Daily loss: total equity across all venues versus the UTC day start (kept across restarts).
  Breaching `max_daily_loss_pct` halts new risk and rotations (hedged positions are kept).
  Deposits, withdrawals and transfers between venues show up as a step in total equity
  between two checks; such steps move the baseline instead of counting as profit or loss.
  A halt also lifts once equity is back within half the limit, or at the next UTC day.
- Positions in markets no pair owns are reported once (manual trades on the bot account).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal

from .config import Config
from .util import ZERO
from .venues.base import VenueError

log = logging.getLogger(__name__)

TRANSFER_STEP_PCT = Decimal("1.5")  # total equity step between two checks treated as money moving


class RiskManager:
    def __init__(self, cfg: Config, engine):
        self.cfg = cfg
        self.engine = engine
        self.day_key: str | None = None
        self.day_start_equity: Decimal | None = None
        self.reported_foreign: set[str] = set()
        self.last_derisk: dict[str, float] = {}
        self.last_total: Decimal | None = None
        self.topup_next: dict[tuple[str, str], float] = {}

    def _pairs_on(self, venue: str) -> list[str]:
        return [p.name for p in self.cfg.pairs if p.enabled and venue in (p.maker.venue, p.hedge.venue)]

    def _pair_for_symbol(self, venue: str, symbol: str) -> str | None:
        for p in self.cfg.pairs:
            if (p.maker.venue, p.maker.symbol) == (venue, symbol) or (p.hedge.venue, p.hedge.symbol) == (venue, symbol):
                return p.name
        return None

    def _derisk(self, pair: str, reason: str, now: float) -> None:
        # One cut per pair every 2 minutes so the reduction and the account refresh can land.
        if now - self.last_derisk.get(pair, 0) < 120:
            return
        self.last_derisk[pair] = now
        self.engine.request_reduction(pair, self.cfg.risk.derisk_fraction, reason)

    def evaluate(self, now: float | None = None) -> None:
        now = now or time.time()
        r = self.cfg.risk
        total = ZERO
        complete = True
        for name in self.engine.venues:
            try:  # one venue being down must not switch off top-ups and de-risk on the others
                acct = self.engine.account(name, self.cfg.poll.account_s)
            except VenueError as exc:
                complete = False
                self.engine.event("warning", "risk", f"{name}: account read failed ({exc}); risk checks skip it",
                                  venue=name, throttle_key=f"riskacct:{name}", throttle_s=600)
                continue
            total += acct.equity
            ratio = acct.margin_ratio
            cross_positions = [p for p in acct.positions.values() if p.margin_mode == "cross"]
            if ratio is not None and cross_positions:
                if ratio < r.margin_ratio_critical:
                    for pair in self._pairs_on(name):
                        self._derisk(pair, f"{name} margin ratio {ratio:.2f}", now)
                    self.engine.event("critical", "margin", f"{name}: margin ratio {ratio:.2f} < {r.margin_ratio_critical}"
                                      " - reducing positions", venue=name, notify=True,
                                      throttle_key=f"mcrit:{name}", throttle_s=300)
                elif ratio < r.margin_ratio_warn:
                    need = acct.maintenance_margin * r.margin_ratio_warn - acct.equity
                    self.engine.event("warning", "margin", f"{name}: margin ratio {ratio:.2f}. Deposit about "
                                      f"${need:,.0f} to {name} to get back to {r.margin_ratio_warn}x",
                                      venue=name, notify=True, throttle_key=f"mwarn:{name}", throttle_s=3600)
            for symbol, pos in acct.positions.items():
                pair = self._pair_for_symbol(name, symbol)
                if pair is None:
                    if (name, symbol) not in self.reported_foreign:
                        self.reported_foreign.add((name, symbol))
                        self.engine.event("warning", "foreign_position",
                                          f"{name}: position in {symbol} is not managed by any pair", venue=name,
                                          notify=True)
                    continue
                if pos.liquidation_price is None or pos.mark_price <= 0 or pos.margin_mode != "isolated":
                    continue
                distance = abs(pos.mark_price - pos.liquidation_price) / pos.mark_price * 100
                if distance < r.isolated_liq_buffer_warn_pct and r.isolated_topup_enabled \
                        and now >= self.topup_next.get((name, symbol), 0):
                    ok = self._top_up(name, acct, pos, pair)
                    self.topup_next[(name, symbol)] = now + (10 if ok else 60)  # no tight retry loop on failure
                    if ok:
                        continue
                if distance < r.isolated_liq_buffer_critical_pct:
                    self._derisk(pair, f"{symbol} {distance:.1f}% from liquidation", now)
                    self.engine.event("critical", "liq_distance",
                                      f"{name} {symbol}: {distance:.1f}% from liquidation - reducing", venue=name,
                                      pair=pair, notify=True, throttle_key=f"lcrit:{name}:{symbol}", throttle_s=300)
                elif distance < r.isolated_liq_buffer_warn_pct:
                    self.engine.event("warning", "liq_distance",
                                      f"{name} {symbol}: {distance:.1f}% from liquidation - add margin", venue=name,
                                      pair=pair, notify=True, throttle_key=f"lwarn:{name}:{symbol}", throttle_s=3600)
        if complete:  # a missing venue would look like a loss
            self._daily_loss(total)

    def _top_up(self, venue_name: str, acct, pos, pair: str) -> bool:
        """Push the liquidation price of an isolated position back to `isolated_topup_target_pct`.

        Moving $1 into the position shifts its liquidation price by 1 / (|size| * (1 -/+ mmf)).
        Returns True when the buffer was fully restored.
        """
        r = self.cfg.risk
        venue = self.engine.venues[venue_name]
        mmf = venue.market(pos.symbol).maintenance_margin_fraction
        target = r.isolated_topup_target_pct / 100
        if pos.size > 0:
            liq_target = pos.mark_price * (1 - target)
            need = (pos.liquidation_price - liq_target) * pos.size * (1 - mmf)
        else:
            liq_target = pos.mark_price * (1 + target)
            need = (liq_target - pos.liquidation_price) * abs(pos.size) * (1 + mmf)
        if need <= 0:
            return True
        amount = min(need, acct.free_collateral * Decimal("0.95")).quantize(Decimal("0.01"))
        if amount < 5:
            return False
        try:
            venue.add_isolated_margin(pos.symbol, amount)
        except VenueError as exc:
            self.engine.event("warning", "topup", f"{venue_name} {pos.symbol}: margin top-up failed: {exc}",
                              venue=venue_name, pair=pair, throttle_key=f"topup:{venue_name}", throttle_s=1800)
            return False
        self.engine.invalidate_account(venue_name)
        full = amount >= need
        self.engine.event("warning", "topup", f"{venue_name} {pos.symbol}: added ${amount:,.2f} isolated margin"
                          f"{'' if full else f' (needed ${need:,.0f}; free balance ran out - deposit more)'}",
                          venue=venue_name, pair=pair, notify=True)
        return full

    def _daily_loss(self, total: Decimal) -> None:
        key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        journal = getattr(self.engine, "journal", None)
        if self.day_key is None and journal is not None:
            saved = journal.load_pair_state("__risk__") or {}
            if saved.get("day") == key:
                self.day_key, self.day_start_equity = key, Decimal(saved["start_equity"])
        if key != self.day_key or self.day_start_equity is None or self.day_start_equity <= 0:
            self.day_key, self.day_start_equity, self.last_total = key, total, total
            self._save_day(journal)
            if self.engine.halted and self.engine.halt_reason == "daily loss":
                self.engine.resume("new UTC day")
            return
        last, self.last_total = self.last_total, total
        if last is not None and last > 0 and abs(total - last) / last * 100 >= TRANSFER_STEP_PCT:
            # A hedged book does not gain or lose this much between two checks: money moved in or out.
            self.day_start_equity += total - last
            self._save_day(journal)
            log.info("total equity stepped %+.0f (deposit / withdrawal / transfer): daily-loss baseline now %.0f",
                     total - last, self.day_start_equity)
            return
        loss_pct = (self.day_start_equity - total) / self.day_start_equity * 100
        limit = self.cfg.risk.max_daily_loss_pct
        if loss_pct >= limit and not self.engine.halted:
            self.engine.halt("daily loss", f"equity down {loss_pct:.2f}% today "
                                           f"(${self.day_start_equity:,.0f} -> ${total:,.0f}). If you are moving "
                                           "money between venues this lifts when it lands")
        elif self.engine.halted and self.engine.halt_reason == "daily loss" and loss_pct < limit / 2:
            self.engine.resume(f"equity back to {-loss_pct:+.2f}% of the day start")

    def _save_day(self, journal) -> None:
        if journal is not None:
            journal.save_pair_state("__risk__", {"day": self.day_key, "start_equity": str(self.day_start_equity)})

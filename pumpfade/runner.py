"""The paper loop: daily scan right after the 00:00 UTC close, minute-by-minute position tracking, reports."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from hedgebot.notifier import Notifier

from .binance import BinancePublic
from .config import Settings
from .listings import ListingWatcher
from .paper import LATE_MS, PaperEngine
from .report import render
from .signals import DAY_MS, HOUR_MS, run_scan
from .store import Store

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))


def kst(day: int) -> str:
    """The KST date on which the UTC day `day` closes (its 00:00 UTC close is 09:00 KST the next day)."""
    return datetime.fromtimestamp((day + 1) * DAY_MS / 1000, KST).strftime("%Y-%m-%d")


class Runner:
    def __init__(self, settings: Settings, client=None, store: Store | None = None, notifier=None, clock=None,
                 listing_venues: dict | None = None, trader=None):
        self.settings = settings
        self.client = client or BinancePublic(settings.base_url)
        self.store = store or Store(settings.db_path)
        self.notifier = notifier or Notifier(settings.telegram_token, settings.telegram_chat,
                                             prefix="[pumpfade LIVE] " if trader else "[pumpfade] ")
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.live = trader is not None
        if self.live:
            from .live import LiveEngine
            self.engine = LiveEngine(trader, self.client, self.store, settings, notify=self.notifier.send,
                                     clock=self.clock)
        else:
            self.engine = PaperEngine(self.client, self.store, settings, notify=self.notifier.send, clock=self.clock)
        self.commands = None
        if settings.telegram_token and settings.telegram_chat:
            from .tgcmd import TelegramCommands
            self.commands = TelegramCommands(settings.telegram_token, settings.telegram_chat, self.store,
                                             {"status": self.status_text, "report": self.report})
        self.watcher = (ListingWatcher(self.store, self.client, settings, venues=listing_venues,
                                       notify=self.notifier.send, clock=self.clock)
                        if settings.watch_listings else None)
        for b in settings.books:
            self.store.book_equity(b.name, settings.start_equity)

    def scan_and_enter(self, day: int) -> str:
        cands, majors = run_scan(self.client, self.settings.strategy, day)
        for c in cands:
            self.store.log_signal(day, c.symbol, c.as_row())
        notes = self.engine.open_signals(cands, day)
        lines = [f"{kst(day)} 09:00 KST scan: majors {majors:+.1%}, {len(cands)} coins up 20%+, "
                 f"{sum(c.passed for c in cands)} passed"]
        for c in cands:
            lines.append(f"  {c.symbol} {c.change:+.0%} rank {c.rank} → "
                         + ("PASS" if c.passed else c.reason))
        lines += [f"  {n}" for n in notes]
        return "\n".join(lines)

    def step(self) -> None:
        now = self.clock()
        today = now // DAY_MS
        target = today - 1
        since = now - today * DAY_MS
        last = self.store.get("last_scan_day")
        if last is None:  # first start: never enter late, start with the next close
            last = target if since > self.settings.scan_deadline_s * 1000 else target - 1
            self.store.put("last_scan_day", last)
        if last < target and since >= self.settings.scan_delay_s * 1000:
            if since <= self.settings.scan_deadline_s * 1000:
                try:
                    self.notifier.send(self.scan_and_enter(target))
                    self.store.put("last_scan_day", target)
                except Exception as exc:  # e.g. the network dropped: try again on the next step until the deadline
                    log.exception("scan failed")
                    self.notifier.send(f"scan for {kst(target)} failed, retrying until 09:30 KST: {exc}", level="error",
                                       key=f"scan-{target}", throttle_s=3600)
            else:
                self.notifier.send(f"missed the {kst(target)} 09:00 KST scan (the bot was not running, or the scan "
                                   f"kept failing until 09:30)", level="warning")
                self.store.put("last_scan_day", target)
        if self.watcher is not None:
            try:
                self.watcher.poll()
                for row in self.watcher.due(now):
                    if now - row["entry_ms"] <= LATE_MS:
                        notes = self.engine.open_listing(row["perp"], row["venue"], row["entry_ms"])
                        row["status"] = "entered"
                        self.notifier.send("\n".join([f"listing short {row['perp']} ({row['venue']})"] + notes))
                    else:
                        row.update(status="skipped", reason="the bot was not running at the entry time")
                    self.store.save_listing(row)
            except Exception as exc:
                log.exception("listing watcher failed")
                self.notifier.send(f"listing watcher failed: {exc}", level="warning", key="listings", throttle_s=3600)
        self.engine.tick()
        if self.commands is not None:
            try:
                self.commands.poll()
            except Exception:
                log.exception("telegram commands failed")
        if now - (self.store.get("funding_ms") or 0) >= HOUR_MS:
            self.engine.settle_all_funding()
            self.store.put("funding_ms", now)
        self.heartbeat(now)
        if (self.store.get("report_day") or 0) < today and since >= 35 * 60_000:
            self.notifier.send(("daily live report\n" if self.live else "daily paper report\n") + self.report())
            self.store.put("report_day", today)

    def heartbeat(self, now: int) -> None:
        """Tell an outside monitor the loop is alive. Skipped when a step fails, so a broken bot also alerts."""
        url = self.settings.heartbeat_url
        if not url or now - getattr(self, "_heartbeat_ms", 0) < 5 * 60_000:
            return
        try:
            import requests
            requests.get(url, timeout=10)
            self._heartbeat_ms = now
        except Exception as exc:
            log.warning("heartbeat ping failed: %s", type(exc).__name__)

    def status_text(self) -> str:
        """Equity and open positions, for /status."""
        now = self.clock()
        last = self.store.get("last_scan_day")
        lines = [f"last 09:00 scan: {kst(last) if last is not None else 'none yet'}"]
        if self.live:
            from .live import ACTIVE
            rs = self.engine.risk_state()
            lines.insert(0, f"equity ${rs['equity']:,.2f} ({rs['dd']:+.1%} from peak), size {rs['fraction']:.1%}"
                         + (f", NEW ENTRIES STOPPED: {rs['halted']}" if rs["halted"] else ""))
            active = self.engine.book.all(ACTIVE)
            for p in active:
                left = f", closes in {(p.deadline_ms - now) / HOUR_MS:.0f}h" if p.deadline_ms else ""
                lines.append(f"{p.symbol} ({p.sleeve}, {p.status}) qty {p.qty:g} entry {p.entry_avg or 0:.6g} "
                             f"stop {p.sl_px or 0:.6g} TP {p.tp_px or 0:.6g}{left}")
        else:
            active = self.store.active_legs()
            for lg in active:
                lines.append(f"{lg.book} {lg.symbol} {lg.kind} {lg.status}")
        if not active:
            lines.append("no open positions")
        return "\n".join(lines)

    def report(self) -> str:
        if self.live:
            return self.engine.report()
        return render(self.store, [b.name for b in self.settings.books], self.settings.start_equity)

    def run_forever(self, interval_s: float = 30.0) -> None:
        if self.live:
            self.notifier.send("LIVE bot started\n" + self.engine.report())
        else:
            self.notifier.send(f"paper bot started, books: {', '.join(b.name for b in self.settings.books)}")
        while True:
            try:
                self.step()
            except Exception as exc:
                log.exception("step failed")
                self.notifier.send(f"step failed: {exc}", level="error", key="step", throttle_s=3600)
            time.sleep(interval_s)

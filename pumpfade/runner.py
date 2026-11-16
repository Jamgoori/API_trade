"""The paper loop: daily scan right after the 00:00 UTC close, minute-by-minute position tracking, reports."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from hedgebot.notifier import Notifier

from .binance import BinancePublic
from .config import Settings
from .paper import PaperEngine
from .report import render
from .signals import DAY_MS, HOUR_MS, run_scan
from .store import Store

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))


def kst(day: int) -> str:
    """The KST date on which the UTC day `day` closes (its 00:00 UTC close is 09:00 KST the next day)."""
    return datetime.fromtimestamp((day + 1) * DAY_MS / 1000, KST).strftime("%Y-%m-%d")


class Runner:
    def __init__(self, settings: Settings, client=None, store: Store | None = None, notifier=None, clock=None):
        self.settings = settings
        self.client = client or BinancePublic(settings.base_url)
        self.store = store or Store(settings.db_path)
        self.notifier = notifier or Notifier(settings.telegram_token, settings.telegram_chat, prefix="[pumpfade] ")
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.engine = PaperEngine(self.client, self.store, settings, notify=self.notifier.send, clock=self.clock)
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
                except Exception as exc:
                    log.exception("scan failed")
                    self.notifier.send(f"scan for {kst(target)} failed: {exc}", level="error")
            else:
                self.notifier.send(f"missed the {kst(target)} 09:00 KST scan (bot was not running)", level="warning")
            self.store.put("last_scan_day", target)
        self.engine.tick()
        if now - (self.store.get("funding_ms") or 0) >= HOUR_MS:
            self.engine.settle_all_funding()
            self.store.put("funding_ms", now)
        if (self.store.get("report_day") or 0) < today and since >= 35 * 60_000:
            self.notifier.send("daily paper report\n" + self.report())
            self.store.put("report_day", today)

    def report(self) -> str:
        return render(self.store, [b.name for b in self.settings.books], self.settings.start_equity)

    def run_forever(self, interval_s: float = 30.0) -> None:
        self.notifier.send(f"paper bot started, books: {', '.join(b.name for b in self.settings.books)}")
        while True:
            try:
                self.step()
            except Exception as exc:
                log.exception("step failed")
                self.notifier.send(f"paper step failed: {exc}", level="error", key="step", throttle_s=3600)
            time.sleep(interval_s)

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
PAUSE_ALERT_S = 300  # a loop gap longer than this means the computer slept or the process was frozen


def keep_awake() -> bool:
    """Ask Windows not to sleep on idle while the bot runs (no effect elsewhere).

    Modern Standby laptops enter standby as soon as the display turns off on idle, so the display is held on too;
    with the lid closed the panel is off anyway. Lid and power-button actions still apply, so the power plan must
    say "do nothing" when the lid closes."""
    import os
    if os.name != "nt":
        return False
    try:
        import ctypes
        es_continuous, es_system_required, es_display_required = 0x80000000, 0x00000001, 0x00000002
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(
            es_continuous | es_system_required | es_display_required))
    except Exception:
        log.warning("could not ask Windows to stay awake", exc_info=True)
        return False


def pause_note(last_s: float, now_s: float) -> str | None:
    """A warning when the loop was not running for a while (sleep, hibernation, frozen process)."""
    gap = now_s - last_s
    if gap < PAUSE_ALERT_S:
        return None
    a = datetime.fromtimestamp(last_s, KST).strftime("%m-%d %H:%M")
    b = datetime.fromtimestamp(now_s, KST).strftime("%m-%d %H:%M")
    return (f"⚠️ the bot was paused for {gap / 3600:.1f}h ({a} → {b} KST). The computer was probably asleep: "
            "check the power settings (sleep never, lid closes → do nothing)")


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
        from .paperlong import PaperLongs
        self.paper_longs = PaperLongs(self.client, self.store, notify=self.notifier.send, clock=self.clock)
        self.commands = None
        if settings.telegram_token and settings.telegram_chat:
            from .tgcmd import TelegramCommands
            self.commands = TelegramCommands(settings.telegram_token, settings.telegram_chat, self.store,
                                             {"status": self.status_text, "report": self.report,
                                              "scan": self.scan_text})
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
        try:
            notes += self.paper_longs.open(cands, day, majors)
        except Exception:
            log.exception("paper longs failed")
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
        try:
            self.paper_longs.tick()
        except Exception:
            log.exception("paper longs failed")
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

    def scan_text(self) -> str:
        """What the 09:00 KST scan would pick if the UTC day closed now, for /scan (read-only)."""
        from .signals import preview_scan
        now = self.clock()
        if now % DAY_MS >= DAY_MS - 10 * 60_000 or now % DAY_MS < 10 * 60_000:
            return "08:50~09:10에는 09:00 진입 때문에 스캔을 안 해요. 09:10 이후에 다시 보내주세요"
        cands, majors, close = preview_scan(self.client, self.settings.strategy, now)
        st = self.settings.strategy
        lines = [f"지금 기준 {kst(now // DAY_MS)} 09:00 진입 후보 (일봉 마감 전이라 바뀔 수 있어요)",
                 f"메이저 평균 {majors:+.2%} (허용 {st.majors_min:+.0%}~{st.majors_max:+.0%})"]
        if not cands:
            lines.append(f"+{st.trigger:.0%} 이상 코인 없음")
        for c in cands:
            ratio = f" 롱숏 {c.ratio:.2f}" if c.ratio is not None else ""
            size = ""
            if c.passed:
                size = " → 12%" if (c.ratio or 0) > st.full_size_ratio else " → 6%"
            lines.append(f"{'✅' if c.passed else '❌'} {c.symbol} {c.change:+.1%} 순위 {c.rank}{ratio}{size}"
                         + ("" if c.passed else f" ({c.reason})"))
        if close:
            lines.append("근접: " + ", ".join(f"{s} {chg:+.1%}" for s, chg in close[:8]))
        for row in self.store.listings("scheduled"):
            lines.append(f"상장빔 예정: {row['perp']} ({row['venue']}) {time.strftime('%m-%d %H:%M', time.gmtime(row['entry_ms'] / 1000 + 9 * 3600))} KST")
        return "\n".join(lines)

    def report(self) -> str:
        if self.live:
            return self.engine.report() + "\n" + self.paper_longs.summary()
        return render(self.store, [b.name for b in self.settings.books], self.settings.start_equity)

    def run_forever(self, interval_s: float = 30.0) -> None:
        if keep_awake():
            log.info("Windows will not sleep on idle while the bot runs")
        if self.live:
            self.notifier.send("LIVE bot started\n" + self.engine.report())
        else:
            self.notifier.send(f"paper bot started, books: {', '.join(b.name for b in self.settings.books)}")
        last, pending = time.time(), None
        while True:
            pending = pause_note(last, time.time()) or pending
            try:
                self.step()
                if pending:  # sent once a step works, i.e. once the network is back after waking up
                    self.notifier.send(pending, level="warning")
                    pending = None
            except Exception as exc:
                log.exception("step failed")
                self.notifier.send(f"step failed: {exc}", level="error", key="step", throttle_s=3600)
            last = time.time()
            time.sleep(interval_s)

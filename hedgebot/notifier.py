"""Alerts: always logged, optionally pushed to Telegram, throttled per key."""

from __future__ import annotations

import logging
import threading
import time

import requests

log = logging.getLogger("hedgebot.alert")


class Notifier:
    def __init__(self, telegram_token: str | None = None, telegram_chat: str | None = None, *, prefix: str = ""):
        self.token = telegram_token
        self.chat = telegram_chat
        self.prefix = prefix
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.token and self.chat)

    def send(self, message: str, *, level: str = "info", key: str | None = None, throttle_s: float = 0) -> None:
        """Log and push. With `key`, repeats inside `throttle_s` are logged but not pushed."""
        text = f"{self.prefix}{message}"
        getattr(log, "error" if level == "critical" else level if level in ("info", "warning", "error") else "info")(text)
        if key and throttle_s:
            with self._lock:
                last = self._last.get(key, 0.0)
                if time.time() - last < throttle_s:
                    return
                self._last[key] = time.time()
        if self.telegram_enabled and level in ("warning", "error", "critical", "info"):
            icon = {"critical": "🚨", "error": "❗", "warning": "⚠️"}.get(level, "ℹ️")
            thread = threading.Thread(target=self._post, args=(f"{icon} {text}",), daemon=True)
            thread.start()
            with self._lock:
                self._threads = [t for t in self._threads if t.is_alive()] + [thread]

    def flush(self, timeout_s: float = 10) -> None:
        """Wait for queued messages (call before the process exits, or the last alerts are lost)."""
        deadline = time.time() + timeout_s
        with self._lock:
            threads = list(self._threads)
        for t in threads:
            t.join(max(0.0, deadline - time.time()))

    def _post(self, text: str) -> None:
        for attempt in range(2):
            try:
                resp = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                                     json={"chat_id": self.chat, "text": text[:4000]}, timeout=10)
            except requests.RequestException as exc:
                log.warning("telegram send failed: %s", exc)
                return
            if resp.status_code == 429 and attempt == 0:
                try:
                    wait = float(resp.json().get("parameters", {}).get("retry_after", 3))
                except ValueError:
                    wait = 3.0
                time.sleep(min(wait, 30))
                continue
            if resp.status_code != 200:
                log.warning("telegram send failed: HTTP %s %s", resp.status_code, resp.text[:200])
            return

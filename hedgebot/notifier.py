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
            threading.Thread(target=self._post, args=(f"{icon} {text}",), daemon=True).start()

    def _post(self, text: str) -> None:
        try:
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                          json={"chat_id": self.chat, "text": text[:4000]}, timeout=10)
        except requests.RequestException as exc:
            log.warning("telegram send failed: %s", exc)

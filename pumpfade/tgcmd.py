"""Read-only Telegram commands: /status, /report and /scan, answered only in the configured chat.

The bot polls getUpdates (outbound HTTPS only, no open ports). Nothing here can place, change or cancel orders.
"""

from __future__ import annotations

import json
import logging

import requests

log = logging.getLogger(__name__)

ALIASES = {
    "/status": "status", "status": "status", "상태": "status", "/상태": "status",
    "/report": "report", "report": "report", "성과": "report", "/성과": "report", "보고": "report",
    "/scan": "scan", "scan": "scan", "스캔": "scan", "/스캔": "scan", "지금": "scan",
    "/help": "help", "help": "help", "/start": "help", "도움": "help", "/도움": "help",
}
HELP = ("조회만 할 수 있어요 (주문은 텔레그램으로 못 해요)\n"
        "/status 또는 상태: 자산과 지금 포지션\n"
        "/report 또는 성과: 성과 요약\n"
        "/scan 또는 스캔: 지금 마감하면 09:00에 잡힐 코인 (롱숏비율 포함, 1분쯤 걸려요)")


def parse(text: str | None) -> str | None:
    if not text:
        return None
    word = text.strip().split()[0].lower().split("@")[0]  # "/status@my_bot" -> "/status"
    return ALIASES.get(word)


class TelegramCommands:
    def __init__(self, token: str, chat: str, store, handlers: dict, timeout: float = 10):
        self.base = f"https://api.telegram.org/bot{token}"
        self.token = token
        self.chat = str(chat)
        self.store = store
        self.handlers = handlers
        self.timeout = timeout

    def _redact(self, text) -> str:
        return str(text).replace(self.token, "<token>")

    def poll(self) -> int:
        """Answer new commands from the owner's chat. Returns how many were answered."""
        params = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
        offset = self.store.get("tg_offset")
        if offset:
            params["offset"] = offset
        try:
            body = requests.get(f"{self.base}/getUpdates", params=params, timeout=self.timeout).json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("telegram poll failed: %s", self._redact(exc))
            return 0
        if not body.get("ok"):
            log.warning("telegram poll refused: %s", body.get("description"))
            return 0
        answered = 0
        for u in body.get("result", []):
            self.store.put("tg_offset", int(u["update_id"]) + 1)
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != self.chat:
                continue  # strangers who find the bot get no answer
            cmd = parse(msg.get("text"))
            try:
                reply = self.handlers[cmd]() if cmd in self.handlers else HELP
            except Exception as exc:
                log.exception("telegram command %s failed", cmd)
                reply = f"could not answer: {exc}"
            self.send(reply)
            answered += 1
        return answered

    def send(self, text: str) -> None:
        try:
            requests.post(f"{self.base}/sendMessage", json={"chat_id": self.chat, "text": text[:4000]},
                          timeout=self.timeout)
        except requests.RequestException as exc:
            log.warning("telegram reply failed: %s", self._redact(exc))

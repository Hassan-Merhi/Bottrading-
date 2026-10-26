"""Telegram alerts, the daily report, and the approve/decline buttons.

Uses the plain Bot HTTP API. Only button presses and commands from the
configured TELEGRAM_CHAT_ID are acted on; everything else is ignored.
"""
from __future__ import annotations

import json
import os
import time
from typing import Callable

import requests

from bots.risk.guard import NullAlerter

API = "https://api.telegram.org/bot{token}/{method}"
NEVER_THROTTLED = {"graduation", "report"}


class TelegramAlerter:
    def __init__(self, token: str, chat_id: str, throttle_minutes: float = 60,
                 session=None, clock: Callable[[], float] = time.time):
        self.token = token
        self.chat_id = str(chat_id)
        self.throttle_s = throttle_minutes * 60
        self.session = session or requests.Session()
        self.clock = clock
        self._sent: dict[tuple[str, str], float] = {}
        self._offset = 0

    def _call(self, method: str, **payload) -> dict:
        r = self.session.post(API.format(token=self.token, method=method), json=payload,
                              timeout=20)
        r.raise_for_status()
        return r.json()

    def alert(self, text: str, kind: str = "info", buttons: list | None = None) -> None:
        key = (kind, text)
        now = self.clock()
        if kind not in NEVER_THROTTLED and now - self._sent.get(key, -1e18) < self.throttle_s:
            return
        self._sent[key] = now
        prefix = {"critical": "🚨 ", "risk": "⚠️ ", "graduation": "🎓 "}.get(kind, "")
        payload: dict = {"chat_id": self.chat_id, "text": prefix + text}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[
                {"text": label, "callback_data": data} for label, data in buttons]]}
        try:
            self._call("sendMessage", **payload)
        except Exception as e:  # noqa: BLE001 - never let alerting crash trading
            print(f"[telegram] send failed: {e}; message was: {text}")

    def send_file(self, path, caption: str = "") -> bool:
        """Send a file to the owner's chat (sendDocument). Never raises."""
        try:
            with open(path, "rb") as fh:
                r = self.session.post(API.format(token=self.token, method="sendDocument"),
                                      data={"chat_id": self.chat_id, "caption": caption[:1000]},
                                      files={"document": (os.path.basename(str(path)), fh)},
                                      timeout=120)
            r.raise_for_status()
            return bool(r.json().get("ok"))
        except Exception as e:  # noqa: BLE001
            print(f"[telegram] file send failed: {e}")
            return False

    def poll(self, on_decision: Callable[[str, bool, str], str],
             on_command: Callable[[str], str] | None = None) -> None:
        """Handle button presses and /commands from the owner's chat."""
        try:
            updates = self._call("getUpdates", offset=self._offset, timeout=0).get("result", [])
        except Exception as e:  # noqa: BLE001
            print(f"[telegram] poll failed: {e}")
            return
        for u in updates:
            self._offset = max(self._offset, u["update_id"] + 1)
            cb = u.get("callback_query")
            if cb:
                chat = str(((cb.get("message") or {}).get("chat") or {}).get("id"))
                if chat != self.chat_id:
                    continue
                action, _, bot_id = (cb.get("data") or "").partition(":")
                if action in ("approve", "decline") and bot_id:
                    reply = on_decision(bot_id, action == "approve", "Telegram")
                    self._safe("answerCallbackQuery", callback_query_id=cb["id"], text=reply[:190])
                    self.alert(reply, kind="report")
                continue
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) == self.chat_id and on_command:
                text = (msg.get("text") or "").strip()
                if text.startswith("/"):
                    self.alert(on_command(text), kind="report")

    def _safe(self, method: str, **payload) -> None:
        try:
            self._call(method, **payload)
        except Exception:  # noqa: BLE001
            pass


def make_alerter(cfg: dict, env: dict | None = None):
    env = env if env is not None else dict(os.environ)
    token, chat = env.get("TELEGRAM_TOKEN"), env.get("TELEGRAM_CHAT_ID")
    if token and chat:
        return TelegramAlerter(token, chat, cfg["telegram"]["alert_throttle_minutes"])
    return NullAlerter()


def dumps(obj) -> str:
    return json.dumps(obj, default=str)

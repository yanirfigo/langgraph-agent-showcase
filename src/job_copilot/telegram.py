"""Minimal Telegram Bot API client: plain text only, owner chat only.

No parse_mode (so no Markdown/HTML injection), link previews off, text capped. Inbound updates
are accepted only from the owner's private chat; everything else is dropped by `is_owner`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from .ingest.models import sanitize

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
MAX_TEXT = 3900  # Telegram's limit is 4096; leave room


@dataclass
class Button:
    text: str
    data: str  # opaque token, <= 64 bytes


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(
        self,
        token: str,
        chat_id: str,
        client: httpx.Client | None = None,
        sleep=time.sleep,
        attempts: int = 3,
    ) -> None:
        self._base = f"{API}/bot{token}"
        self.chat_id = str(chat_id)
        self._http = client or httpx.Client(timeout=70, trust_env=False)
        self._sleep, self._attempts = sleep, attempts

    def _call(self, method: str, payload: dict) -> dict:
        """POST with retries: honours 429 retry_after, retries transient network errors.
        Errors never include the request URL (it contains the token)."""
        last = "error"
        for attempt in range(self._attempts):
            final = attempt == self._attempts - 1
            try:
                body = self._http.post(f"{self._base}/{method}", json=payload).json()
            except (httpx.TransportError, ValueError) as e:
                last = type(e).__name__
                if final:
                    break
                self._sleep(2 * (attempt + 1))
                continue
            except httpx.HTTPError as e:
                raise TelegramError(f"{method} failed: {type(e).__name__}") from None
            if body.get("ok"):
                return body["result"]
            last = str(body.get("description", "error"))
            retry_after = (body.get("parameters") or {}).get("retry_after")
            if body.get("error_code") == 429 and not final:
                self._sleep(min(int(retry_after or 5), 60))
                continue
            break
        raise TelegramError(f"{method}: {last}")

    @staticmethod
    def _keyboard(rows: list[list[Button]]) -> dict:
        return {"inline_keyboard": [[{"text": b.text, "callback_data": b.data} for b in row] for row in rows]}

    def send(self, text: str, buttons: list[list[Button]] | None = None) -> int:
        payload = {
            "chat_id": self.chat_id,
            "text": sanitize(text, MAX_TEXT, multiline=True) or "(empty)",
            "link_preview_options": {"is_disabled": True},
        }
        if buttons:
            payload["reply_markup"] = self._keyboard(buttons)
        return self._call("sendMessage", payload)["message_id"]

    def set_buttons(self, message_id: int | None, buttons: list[list[Button]] | None) -> bool:
        """Best effort: a failed edit only leaves stale buttons, which the bot rejects as 'Already handled'."""
        if message_id is None:
            return False
        markup = self._keyboard(buttons) if buttons else {"inline_keyboard": []}
        try:
            self._call(
                "editMessageReplyMarkup",
                {"chat_id": self.chat_id, "message_id": message_id, "reply_markup": markup},
            )
        except TelegramError as e:
            if "not modified" not in str(e):
                log.warning("could not update buttons: %s", e)
                return False
        return True

    def answer(self, callback_id: str, text: str = "") -> None:
        """Best effort: Telegram rejects answers to old callbacks; the action must still run."""
        try:
            self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:180]})
        except TelegramError as e:
            log.info("callback answer skipped: %s", e)

    def updates(self, offset: int, timeout: int = 50) -> list[dict]:
        return self._call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
        )

    def is_owner(self, update: dict) -> bool:
        """True only for the owner's private chat, both for messages and button presses."""
        if "callback_query" in update:
            cq = update["callback_query"]
            chat = (cq.get("message") or {}).get("chat") or {}
            return str(chat.get("id")) == self.chat_id and str((cq.get("from") or {}).get("id")) == self.chat_id
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        return (
            chat.get("type") == "private"
            and str(chat.get("id")) == self.chat_id
            and str((msg.get("from") or {}).get("id")) == self.chat_id
        )

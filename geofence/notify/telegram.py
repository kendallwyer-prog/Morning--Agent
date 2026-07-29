"""A minimal Telegram Bot API client — stdlib ``urllib`` only, no dependency.

Why long-polling and not a webhook: a webhook needs a second public HTTPS
route, and the ingestion endpoint is deliberately the only thing exposed.
``getUpdates`` runs fine from behind any NAT, on a Pi, with no inbound ports.

Two details that matter for correctness:

* **The update offset is persisted by the caller.** Telegram replays every
  update until you acknowledge it with ``offset = last_id + 1``. Keeping that
  cursor in the database (not in memory) means a restart neither loses your
  button tap nor re-processes it.
* **Network failure is normal, not exceptional.** Every call returns a result
  object instead of raising, so a dropped connection at 5:37am gets retried on
  the next tick rather than killing the agent.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"

# Callback payloads sent by the inline buttons: "<action>:<alert_id>".
CB_ON_MY_WAY = "omw"
CB_SKIPPING = "skip"


@dataclass
class Update:
    """One inbound update, flattened to the two shapes we care about."""

    update_id: int
    kind: str                      # 'callback' | 'text'
    chat_id: str | None = None
    text: str | None = None        # for 'text'
    data: str | None = None        # for 'callback', e.g. "omw:41"
    callback_query_id: str | None = None
    message_id: int | None = None


@dataclass
class SendResult:
    ok: bool
    message_id: int | None = None
    error: str | None = None


@dataclass
class TelegramClient:
    bot_token: str
    chat_id: str
    timeout_sec: int = 30
    # Set by tests to capture calls without touching the network.
    _transport: object | None = field(default=None, repr=False)

    # --- transport -----------------------------------------------------------

    def _call(self, method: str, payload: dict, timeout: int | None = None) -> dict | None:
        """POST to one Bot API method. Returns the `result` field, or None on
        any failure (logged, never raised)."""
        if self._transport is not None:                     # test seam
            return self._transport(method, payload)         # type: ignore[operator]

        url = f"{API_BASE}/bot{self.bot_token}/{method}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout_sec) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:200]
            log.warning("telegram %s failed: HTTP %s %s", method, e.code, detail)
            return None
        except Exception as e:                              # timeout, DNS, TLS...
            log.warning("telegram %s failed: %s", method, e)
            return None

        if not data.get("ok"):
            log.warning("telegram %s returned not-ok: %s", method, data.get("description"))
            return None
        return data.get("result")

    # --- outbound ------------------------------------------------------------

    def send(self, text: str, buttons: list[tuple[str, str]] | None = None) -> SendResult:
        """Send a message, optionally with one row of inline buttons.

        `buttons` is a list of (label, callback_data) pairs.
        """
        payload: dict = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": label, "callback_data": data} for label, data in buttons]
                ]
            }
        result = self._call("sendMessage", payload)
        if result is None:
            return SendResult(ok=False, error="send failed")
        return SendResult(ok=True, message_id=result.get("message_id"))

    def get_me(self) -> dict | None:
        """Identify the bot behind the token. None if the token is rejected."""
        return self._call("getMe", {})

    def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        """Acknowledge a button tap so the client stops showing a spinner."""
        self._call(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text},
        )

    def edit_message(self, message_id: int, text: str) -> None:
        """Rewrite a sent alert (and drop its buttons) once it's answered, so
        the thread shows the decision instead of a stale, tappable prompt."""
        self._call(
            "editMessageText",
            {
                "chat_id": self.chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": []},
            },
        )

    # --- inbound -------------------------------------------------------------

    def get_updates(self, offset: int | None, timeout_sec: int = 25) -> list[Update]:
        """Long-poll for updates. Returns [] on any network failure."""
        payload: dict = {
            "timeout": timeout_sec,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        # HTTP timeout must exceed the long-poll timeout or we'd cut it off.
        result = self._call("getUpdates", payload, timeout=timeout_sec + 10)
        if not result:
            return []
        return [u for u in (parse_update(raw) for raw in result) if u is not None]


def parse_update(raw: dict) -> Update | None:
    """Flatten a Telegram update. Returns None for shapes we don't handle."""
    uid = raw.get("update_id")
    if uid is None:
        return None

    cq = raw.get("callback_query")
    if cq:
        msg = cq.get("message") or {}
        return Update(
            update_id=uid,
            kind="callback",
            chat_id=str((msg.get("chat") or {}).get("id", "")) or None,
            data=cq.get("data"),
            callback_query_id=cq.get("id"),
            message_id=msg.get("message_id"),
        )

    msg = raw.get("message")
    if msg and msg.get("text"):
        return Update(
            update_id=uid,
            kind="text",
            chat_id=str((msg.get("chat") or {}).get("id", "")) or None,
            text=msg["text"],
            message_id=msg.get("message_id"),
        )
    return None


def parse_callback_data(data: str | None) -> tuple[str | None, int | None]:
    """"omw:41" -> ("omw", 41). Returns (None, None) if malformed."""
    if not data or ":" not in data:
        return None, None
    action, _, rest = data.partition(":")
    try:
        return action, int(rest)
    except ValueError:
        return None, None

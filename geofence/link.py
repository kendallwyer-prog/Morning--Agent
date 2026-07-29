"""`telegram link`: connect an existing BotFather bot in one command.

The manual version of this is "message your bot, curl getUpdates, find
``result[0].message.chat.id`` in the JSON, copy it into a file" — four steps
where three can go wrong, at the exact moment someone is deciding whether this
project is worth the trouble. So it's one command that waits for you to say
hello and reads the id itself.

Two details worth knowing:

* **It never acknowledges the update.** ``getUpdates`` is called without an
  offset, so your "hello" stays in the queue and the agent's own cursor is
  untouched. Linking twice, or linking after the agent has run, changes nothing.
* **It writes nothing without being asked.** ``--write`` appends to the env
  file; otherwise it prints the line for you to paste, because a command that
  silently edits a config file on your behalf is a bad surprise.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from .notify.telegram import TelegramClient

#: How long `--wait` keeps listening for your first message, in seconds.
WAIT_TIMEOUT_SEC = 180


@dataclass
class LinkResult:
    ok: bool
    bot_username: str | None = None
    chat_id: str | None = None
    chat_name: str | None = None
    error: str | None = None


def identify_bot(token: str) -> tuple[str | None, str | None]:
    """(username, error) for a token, without needing a chat id yet."""
    me = TelegramClient(bot_token=token, chat_id="").get_me()
    if not me:
        return None, (
            "Telegram rejected that token. Check it against the one BotFather "
            "sent you — it looks like 123456789:AA... with no spaces."
        )
    return me.get("username"), None


def _latest_chat(client: TelegramClient) -> tuple[str, str] | None:
    """(chat_id, display name) from the newest pending message, if any."""
    # No offset: this reads without consuming, so the agent still sees it later.
    updates = client.get_updates(None, timeout_sec=1)
    for u in reversed(updates):
        if u.chat_id:
            return u.chat_id, (u.text or "").strip()[:40] or "your chat"
    return None


def link(token: str, wait: bool = True, timeout_sec: int = WAIT_TIMEOUT_SEC) -> LinkResult:
    """Find the chat id of whoever has messaged this bot."""
    username, err = identify_bot(token)
    if err:
        return LinkResult(False, error=err)

    client = TelegramClient(bot_token=token, chat_id="")
    found = _latest_chat(client)

    if found is None and wait:
        deadline = time.monotonic() + timeout_sec
        while found is None and time.monotonic() < deadline:
            time.sleep(2)
            found = _latest_chat(client)

    if found is None:
        return LinkResult(
            False,
            bot_username=username,
            error=(
                f"No messages yet. Open Telegram, find @{username}, and send it "
                f"anything — a bot can't start the conversation, so it has "
                f"nowhere to send alerts until you do. Then run this again."
            ),
        )

    chat_id, name = found
    return LinkResult(True, bot_username=username, chat_id=chat_id, chat_name=name)


def write_env(path: str, token: str, chat_id: str) -> str:
    """Append the two variables to an env file, replacing any existing pair.

    Creates the file 0600 if absent: it holds a token that IS the bot, so it
    must never be world-readable, and must never be the file you commit.
    """
    lines: list[str] = []
    if os.path.exists(path):
        with open(path) as f:
            lines = [
                ln.rstrip("\n")
                for ln in f
                if not ln.startswith(("TELEGRAM_BOT_TOKEN=", "TELEGRAM_CHAT_ID="))
            ]
    lines.append(f"TELEGRAM_BOT_TOKEN={token}")
    lines.append(f"TELEGRAM_CHAT_ID={chat_id}")

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
    return path

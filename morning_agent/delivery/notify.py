"""Push delivery via ntfy.sh.

A single HTTP POST to https://ntfy.sh/<topic>. The body is the message; headers
carry the title and options. Your iPhone/iPad receive it through the free ntfy
iOS app subscribed to the same topic.

The topic name is effectively a shared secret — anyone who knows it can read and
post. Use a long random topic (see .env.example).
"""
from __future__ import annotations

import requests

from .. import config

# ntfy delivers as a push notification; long bodies are supported but very long
# ones get truncated in the notification preview (full text shows when opened).
_TIMEOUT = 20


def send(title: str, body: str, *, priority: int = 3, tags: list[str] | None = None) -> None:
    """Send a notification. Raises on failure so the caller can react.

    priority: 1 (min) .. 5 (max). 3 is default.
    tags: ntfy emoji shortcodes, e.g. ["sunrise"].
    """
    topic = config.require("NTFY_TOPIC")
    server = config.NTFY_SERVER.rstrip("/")
    url = f"{server}/{topic}"

    headers = {
        "Title": title.encode("utf-8"),
        "Priority": str(priority),
        # Markdown rendering in the ntfy iOS app.
        "Markdown": "yes",
    }
    if tags:
        headers["Tags"] = ",".join(tags)

    resp = requests.post(
        url,
        data=body.encode("utf-8"),
        headers=headers,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()

"""`telegram link` — connecting an existing BotFather bot."""

import os
import stat

import pytest

from geofence import link as link_mod
from geofence.link import write_env


class FakeApi:
    """Scripted Bot API: a token that may be valid, and a message queue."""

    def __init__(self, valid=True, updates=None):
        self.valid = valid
        self.updates = updates or []
        self.calls = []

    def __call__(self, method, payload):
        self.calls.append((method, payload))
        if method == "getMe":
            return {"username": "henrys_agent_bot", "id": 42} if self.valid else None
        if method == "getUpdates":
            # Reading must NOT consume: the agent's own cursor comes later.
            return list(self.updates)
        return None


def _patch(monkeypatch, api):
    real = link_mod.TelegramClient

    def factory(**kwargs):
        return real(**{**kwargs, "_transport": api})

    monkeypatch.setattr(link_mod, "TelegramClient", factory)


def _msg(update_id, chat_id, text):
    return {
        "update_id": update_id,
        "message": {"message_id": update_id, "text": text, "chat": {"id": chat_id}},
    }


def test_a_bad_token_is_reported_in_plain_language(monkeypatch):
    _patch(monkeypatch, FakeApi(valid=False))
    result = link_mod.link("nope", wait=False)
    assert not result.ok
    assert "rejected that token" in result.error
    assert "BotFather" in result.error


def test_it_finds_your_chat_id_from_your_hello(monkeypatch):
    api = FakeApi(updates=[_msg(1, 987654321, "hi")])
    _patch(monkeypatch, api)

    result = link_mod.link("123:AA", wait=False)
    assert result.ok
    assert result.bot_username == "henrys_agent_bot"
    assert result.chat_id == "987654321"


def test_the_newest_message_wins(monkeypatch):
    """Re-linking after switching accounts should pick the latest sender."""
    api = FakeApi(updates=[_msg(1, 111, "old"), _msg(2, 222, "new")])
    _patch(monkeypatch, api)
    assert link_mod.link("123:AA", wait=False).chat_id == "222"


def test_linking_does_not_consume_the_update(monkeypatch):
    """No offset is sent, so your hello stays queued and the agent's cursor
    is untouched — linking twice, or after the agent has run, is harmless."""
    api = FakeApi(updates=[_msg(1, 555, "hi")])
    _patch(monkeypatch, api)
    link_mod.link("123:AA", wait=False)

    polls = [p for m, p in api.calls if m == "getUpdates"]
    assert polls, "expected a getUpdates call"
    assert all("offset" not in p for p in polls)


def test_never_messaged_the_bot_explains_why(monkeypatch):
    """A bot cannot open the conversation — that's the whole gotcha."""
    _patch(monkeypatch, FakeApi(updates=[]))
    result = link_mod.link("123:AA", wait=False)
    assert not result.ok
    assert "@henrys_agent_bot" in result.error
    assert "can't start the conversation" in result.error


def test_wait_gives_up_rather_than_hanging_forever(monkeypatch):
    _patch(monkeypatch, FakeApi(updates=[]))
    monkeypatch.setattr(link_mod.time, "sleep", lambda s: None)
    result = link_mod.link("123:AA", wait=True, timeout_sec=0.1)
    assert not result.ok


# --- env file ---------------------------------------------------------------


def test_env_file_is_written_private(tmp_path):
    path = write_env(str(tmp_path / "geofence.env"), "123:AA", "555")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, "token must not be readable"
    body = open(path).read()
    assert "TELEGRAM_BOT_TOKEN=123:AA" in body
    assert "TELEGRAM_CHAT_ID=555" in body


def test_rewriting_replaces_rather_than_duplicates(tmp_path):
    p = tmp_path / "geofence.env"
    p.write_text("GEOFENCE_SECRET=keepme\nTELEGRAM_BOT_TOKEN=old\nTELEGRAM_CHAT_ID=old\n")

    write_env(str(p), "123:AA", "555")
    body = p.read_text()
    assert body.count("TELEGRAM_BOT_TOKEN=") == 1
    assert body.count("TELEGRAM_CHAT_ID=") == 1
    assert "old" not in body
    assert "GEOFENCE_SECRET=keepme" in body, "must not clobber the ingestion secret"

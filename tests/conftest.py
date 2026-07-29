"""Shared fixtures for the Phase 2 (alerting) tests.

Everything runs against an in-memory SQLite database and a fake Telegram
transport, so the whole agent is testable without a network, a phone, or
waiting for 5:37am.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from geofence.config import Alerting, Coffee, Commitment, Config, Region, Telegram
from geofence.db import connect, init_db
from geofence.ingest.timeutil import iso_utc
from geofence.notify.telegram import TelegramClient

TZ = "America/New_York"


@dataclass
class FakeTelegram:
    """Records outbound calls and replays scripted inbound updates."""

    sent: list[dict] = field(default_factory=list)
    edits: list[dict] = field(default_factory=list)
    answered: list[str] = field(default_factory=list)
    inbox: list[dict] = field(default_factory=list)
    fail_sends: bool = False
    _next_message_id: int = 100

    def __call__(self, method: str, payload: dict):
        if method == "sendMessage":
            if self.fail_sends:
                return None
            self._next_message_id += 1
            self.sent.append(payload)
            return {"message_id": self._next_message_id}
        if method == "editMessageText":
            self.edits.append(payload)
            return {"message_id": payload["message_id"]}
        if method == "answerCallbackQuery":
            self.answered.append(payload.get("text", ""))
            return True
        if method == "getUpdates":
            out, self.inbox = self.inbox, []
            return out
        return None

    @property
    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent]

    def push_callback(self, update_id: int, data: str, chat_id: str = "555"):
        self.inbox.append(
            {
                "update_id": update_id,
                "callback_query": {
                    "id": f"cb{update_id}",
                    "data": data,
                    "message": {"message_id": 101, "chat": {"id": chat_id}},
                },
            }
        )

    def push_text(self, update_id: int, text: str, chat_id: str = "555"):
        self.inbox.append(
            {
                "update_id": update_id,
                "message": {"message_id": 200 + update_id, "text": text,
                            "chat": {"id": chat_id}},
            }
        )


@pytest.fixture
def fake_tg():
    return FakeTelegram()


@pytest.fixture
def client(fake_tg):
    return TelegramClient(bot_token="t", chat_id="555", _transport=fake_tg)


def make_config(**overrides) -> Config:
    regions = {
        rid: Region(id=rid, lat=None, lon=None, radius_m=100, name=name)
        for rid, name in {
            "henry_hall": "Henry Hall",
            "denunzio": "DeNunzio Pool",
            "prospect_club": "the club",
            "nassau_starbucks": "Nassau St Starbucks",
        }.items()
    }
    cfg = Config(
        timezone=TZ,
        db_path=":memory:",
        flap_window_sec=90,
        max_transit_sec=1800,
        silence_threshold_hours=6,
        regions=regions,
        min_transit={},
        secret="s",
        commitments=[
            Commitment(
                id="swim_practice",
                label="Swim practice",
                origin="henry_hall",
                destination="denunzio",
                arrive_by="06:00",
                days=["mon", "tue", "wed", "thu", "fri"],
                prep_sec=900,
                safety_margin_sec=300,
                fallback_travel_sec=600,
            ),
            Commitment(
                id="club_lunch",
                label="Lunch at the club",
                origin="henry_hall",
                destination="prospect_club",
                arrive_by="12:15",
                days=["mon", "tue", "wed", "thu", "fri"],
                prep_sec=600,
                safety_margin_sec=300,
                fallback_travel_sec=600,
            ),
        ],
        alerting=Alerting(),
        coffee=Coffee(),
        telegram=Telegram(bot_token="t", chat_id="555"),
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


@pytest.fixture
def config():
    return make_config()


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    yield c
    c.close()


def add_event(
    conn,
    region: str,
    transition: str,
    when: datetime,
    suppressed: int = 0,
) -> int:
    """Insert a normalized event directly (bypassing HTTP ingestion)."""
    conn.execute(
        "INSERT INTO raw_events (received_at_utc, client_type, dedupe_key, payload_text) "
        "VALUES (?, 'test', ?, '{}')",
        (iso_utc(when), f"{region}|{transition}|{when.isoformat()}"),
    )
    raw_id = conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    cur = conn.execute(
        "INSERT INTO events (raw_id, region, transition, event_utc, event_local, "
        " tz_offset_min, calibrated_utc, suppressed) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
        (raw_id, region, transition, iso_utc(when), iso_utc(when), iso_utc(when),
         suppressed),
    )
    conn.commit()
    return cur.lastrowid


def add_transit(
    conn, origin: str, dest: str, duration_sec: int, start: datetime,
    status: str = "complete",
) -> None:
    """Insert a derived transit segment (what the estimator reads)."""
    a = add_event(conn, origin, "exit", start)
    b = add_event(conn, dest, "enter", start + timedelta(seconds=duration_sec))
    conn.execute(
        "INSERT INTO segments (segment_type, from_region, to_region, start_event_id, "
        " end_event_id, start_utc, end_utc, duration_sec, status) "
        "VALUES ('transit', ?, ?, ?, ?, ?, ?, ?, ?)",
        (origin, dest, a, b, iso_utc(start),
         iso_utc(start + timedelta(seconds=duration_sec)), duration_sec, status),
    )
    conn.commit()


def add_dwell(conn, region: str, duration_sec: int, start: datetime) -> None:
    a = add_event(conn, region, "enter", start)
    b = add_event(conn, region, "exit", start + timedelta(seconds=duration_sec))
    conn.execute(
        "INSERT INTO segments (segment_type, from_region, to_region, start_event_id, "
        " end_event_id, start_utc, end_utc, duration_sec, status) "
        "VALUES ('dwell', ?, ?, ?, ?, ?, ?, ?, 'complete')",
        (region, region, a, b, iso_utc(start),
         iso_utc(start + timedelta(seconds=duration_sec)), duration_sec),
    )
    conn.commit()


def utc(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)

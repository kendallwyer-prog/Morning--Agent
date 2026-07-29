"""SQLite connection + schema.

Three-table design for full re-derivability:

  raw_events  -- IMMUTABLE. Exact payload, written before any processing.
  events      -- DERIVED from raw_events (normalized). Rebuilt by `reprocess`.
  segments    -- DERIVED from events (matched pairs). Rebuilt by `reprocess`.

Because everything downstream of raw_events is derived, you can change your
logic later and re-run `reprocess` to rebuild events + segments from scratch.
"""

from __future__ import annotations

import os
import sqlite3

SCHEMA = """
-- 1. Immutable audit log. Written before any processing. Never UPDATEd.
CREATE TABLE IF NOT EXISTS raw_events (
    id              INTEGER PRIMARY KEY,
    received_at_utc TEXT NOT NULL,           -- server receipt time (ISO-8601 UTC)
    client_type     TEXT NOT NULL,           -- 'shortcuts' | 'owntracks' | 'unknown'
    dedupe_key      TEXT NOT NULL UNIQUE,     -- idempotency key; duplicate => no-op
    payload_text    TEXT NOT NULL,           -- raw body, verbatim, untouched
    source_ip       TEXT
);
CREATE INDEX IF NOT EXISTS ix_raw_received ON raw_events(received_at_utc);

-- 2. Normalized events. DERIVED from raw_events; safe to wipe + rebuild.
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY,
    raw_id         INTEGER NOT NULL REFERENCES raw_events(id),
    region         TEXT NOT NULL,
    transition     TEXT NOT NULL,            -- 'enter' | 'exit'
    event_utc      TEXT NOT NULL,            -- device event time (ISO-8601 UTC)
    event_local    TEXT NOT NULL,            -- local wall-clock (with offset)
    tz_offset_min  INTEGER NOT NULL,         -- offset at that instant => DST captured
    lat            REAL,
    lon            REAL,
    acc            REAL,
    calibrated_utc TEXT NOT NULL,            -- event_utc adjusted by region offset
    suppressed     INTEGER NOT NULL DEFAULT 0 -- 1 = discarded by debounce
);
CREATE INDEX IF NOT EXISTS ix_events_time ON events(event_utc);

-- 3. Derived segments: matched exit-from-A / enter-at-B, plus in-region dwell.
CREATE TABLE IF NOT EXISTS segments (
    id             INTEGER PRIMARY KEY,
    segment_type   TEXT NOT NULL,            -- 'transit' | 'dwell'
    from_region    TEXT NOT NULL,
    to_region      TEXT,                     -- NULL when incomplete transit
    start_event_id INTEGER NOT NULL REFERENCES events(id),
    end_event_id   INTEGER REFERENCES events(id),
    start_utc      TEXT NOT NULL,
    end_utc        TEXT,
    duration_sec   INTEGER,                  -- NULL when incomplete
    status         TEXT NOT NULL,            -- 'complete' | 'incomplete' | 'suspect'
    status_reason  TEXT
);
CREATE INDEX IF NOT EXISTS ix_segments_status ON segments(status);

-- Week-one ground truth: manually entered actual departure times for
-- calibrating the per-region offset against the (late) iOS 'leave' events.
CREATE TABLE IF NOT EXISTS ground_truth (
    id                INTEGER PRIMARY KEY,
    region            TEXT NOT NULL,
    actual_depart_utc TEXT NOT NULL,
    note              TEXT,
    created_at_utc    TEXT NOT NULL
);

-- 4. One row per commitment-occurrence (e.g. "swim_practice on 2026-03-04").
-- The UNIQUE constraint is the whole anti-double-send mechanism: the row is
-- claimed BEFORE the Telegram call, so a crash-restart can never re-ping you.
CREATE TABLE IF NOT EXISTS alerts (
    id                  INTEGER PRIMARY KEY,
    commitment_id       TEXT NOT NULL,
    occurrence_date     TEXT NOT NULL,        -- LOCAL date, YYYY-MM-DD
    kind                TEXT NOT NULL,        -- 'departure' | 'nudge'
    planned_depart_utc  TEXT NOT NULL,        -- when you must actually walk out
    arrive_by_utc       TEXT NOT NULL,
    travel_estimate_sec INTEGER NOT NULL,
    estimate_source     TEXT NOT NULL,        -- 'observed' | 'fallback'
    estimate_n          INTEGER NOT NULL DEFAULT 0,
    sent_at_utc         TEXT,                 -- NULL = claimed but not yet sent
    telegram_message_id INTEGER,
    response            TEXT,                 -- 'on_my_way' | 'skipping'
    responded_at_utc    TEXT,
    actual_depart_utc   TEXT,                 -- filled in by grading
    actual_arrive_utc   TEXT,
    outcome             TEXT,                 -- see grading.OUTCOMES
    graded_at_utc       TEXT,
    UNIQUE(commitment_id, occurrence_date, kind)
);
CREATE INDEX IF NOT EXISTS ix_alerts_pending ON alerts(outcome, occurrence_date);

-- 5. Tiny key/value store for the Telegram getUpdates offset and similar
-- cursors. Persisted so a restart neither replays nor drops your button taps.
CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")       # survive reboots / concurrent reads
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()

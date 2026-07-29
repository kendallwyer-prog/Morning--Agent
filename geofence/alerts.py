"""Persistence for alerts and bot cursors.

The one rule that matters here: **an alert row is claimed before the Telegram
call is made, never after.** The ``UNIQUE(commitment_id, occurrence_date,
kind)`` constraint plus ``INSERT OR IGNORE`` means "claim" is atomic — so a
crash-restart loop, two agents started by mistake, or a tick that runs twice in
the same second can physically not ping you twice for the same practice.

A row therefore has three lives:
  claimed  (sent_at_utc IS NULL)  -> a send is in flight or failed
  sent     (sent_at_utc set)      -> waiting on your tap
  graded   (outcome set)          -> closed, and feeding the advice engine
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .ingest.timeutil import iso_utc
from .schedule import Occurrence

# Response values written by the Telegram buttons.
ON_MY_WAY = "on_my_way"
SKIPPING = "skipping"


def claim(conn: sqlite3.Connection, occ: Occurrence, kind: str = "departure") -> int | None:
    """Reserve the right to send one alert. Returns the new alert id, or None
    if this occurrence+kind was already claimed by someone else."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO alerts "
        "(commitment_id, occurrence_date, kind, planned_depart_utc, arrive_by_utc, "
        " travel_estimate_sec, estimate_source, estimate_n) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            occ.commitment.id,
            occ.occurrence_date,
            kind,
            iso_utc(occ.must_leave_utc),
            iso_utc(occ.arrive_by_utc),
            occ.estimate.seconds,
            occ.estimate.source,
            occ.estimate.n,
        ),
    )
    conn.commit()
    return cur.lastrowid if cur.rowcount else None


def exists(
    conn: sqlite3.Connection, commitment_id: str, occurrence_date: str, kind: str = "departure"
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM alerts WHERE commitment_id = ? AND occurrence_date = ? AND kind = ?",
        (commitment_id, occurrence_date, kind),
    ).fetchone()
    return row is not None


def get(conn: sqlite3.Connection, alert_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()


def mark_sent(
    conn: sqlite3.Connection, alert_id: int, message_id: int | None, now: datetime | None = None
) -> None:
    now = now or datetime.now(timezone.utc)
    conn.execute(
        "UPDATE alerts SET sent_at_utc = ?, telegram_message_id = ? WHERE id = ?",
        (iso_utc(now), message_id, alert_id),
    )
    conn.commit()


def drop(conn: sqlite3.Connection, alert_id: int) -> None:
    """Release a claim whose send failed, so the next tick can retry it (the
    ``max_late_send_sec`` window still applies, so it can't retry forever)."""
    conn.execute("DELETE FROM alerts WHERE id = ? AND sent_at_utc IS NULL", (alert_id,))
    conn.commit()


def record_response(
    conn: sqlite3.Connection, alert_id: int, response: str, now: datetime | None = None
) -> bool:
    """Record a button tap. Returns False if this alert was already answered —
    tapping twice must not overwrite the first, honest answer."""
    now = now or datetime.now(timezone.utc)
    cur = conn.execute(
        "UPDATE alerts SET response = ?, responded_at_utc = ? "
        "WHERE id = ? AND response IS NULL",
        (response, iso_utc(now), alert_id),
    )
    if response == SKIPPING and cur.rowcount:
        # A skip closes the occurrence immediately: no nudge, no grading, and
        # crucially it is NOT counted as a late arrival by the advice engine.
        conn.execute(
            "UPDATE alerts SET outcome = 'skipped', graded_at_utc = ? WHERE id = ?",
            (iso_utc(now), alert_id),
        )
    conn.commit()
    return bool(cur.rowcount)


def set_outcome(
    conn: sqlite3.Connection,
    alert_id: int,
    outcome: str,
    actual_depart_utc: datetime | None = None,
    actual_arrive_utc: datetime | None = None,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(timezone.utc)
    conn.execute(
        "UPDATE alerts SET outcome = ?, actual_depart_utc = ?, actual_arrive_utc = ?, "
        "graded_at_utc = ? WHERE id = ?",
        (
            outcome,
            iso_utc(actual_depart_utc) if actual_depart_utc else None,
            iso_utc(actual_arrive_utc) if actual_arrive_utc else None,
            iso_utc(now),
            alert_id,
        ),
    )
    conn.commit()


def ungraded(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Sent departure alerts still waiting on an outcome."""
    return conn.execute(
        "SELECT * FROM alerts WHERE kind = 'departure' AND outcome IS NULL "
        "AND sent_at_utc IS NOT NULL ORDER BY arrive_by_utc"
    ).fetchall()


def graded(
    conn: sqlite3.Connection, commitment_id: str, limit: int = 50
) -> list[sqlite3.Row]:
    """Most recent graded occurrences for one commitment, newest first."""
    return conn.execute(
        "SELECT * FROM alerts WHERE commitment_id = ? AND kind = 'departure' "
        "AND outcome IS NOT NULL ORDER BY arrive_by_utc DESC LIMIT ?",
        (commitment_id, limit),
    ).fetchall()


def open_alert_for_today(
    conn: sqlite3.Connection, occurrence_date: str
) -> sqlite3.Row | None:
    """The most recent unanswered departure alert on a given local date —
    used so a plain text reply ("omw") can be attributed without a button."""
    return conn.execute(
        "SELECT * FROM alerts WHERE occurrence_date = ? AND kind = 'departure' "
        "AND sent_at_utc IS NOT NULL AND response IS NULL "
        "ORDER BY sent_at_utc DESC LIMIT 1",
        (occurrence_date,),
    ).fetchone()


# --- bot cursors -------------------------------------------------------------


def state_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def state_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO bot_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()

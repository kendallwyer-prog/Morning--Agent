"""Closing the loop: did you actually make it, and when did you actually leave?

This is the "collect data behind the scenes" half. Every sent alert eventually
gets an outcome by matching it against the geofence events that arrived on
their own — you are never asked to confirm anything.

Grading deliberately waits until ``settle_sec`` after the arrival deadline
before judging: events arrive when the phone gets round to sending them, and an
alert graded the instant the clock ticks past 6:00am would score a perfectly
punctual arrival as a no-show.

Outcome vocabulary, and why each exists:

  ``on_time``       arrived at or before ``arrive_by`` (+ grace).
  ``late``          arrived after. Feeds "leave earlier" advice.
  ``skipped``       you tapped "skipping today". Written at tap time, NOT here.
                    Excluded from every timing statistic — a skip is a choice,
                    not a failure, and counting it as lateness would slowly
                    push your departure times earlier for no reason.
  ``no_show``       no arrival, and you never said you were skipping. Kept
                    separate from ``late`` so a forgotten tap can't masquerade
                    as chronic lateness.
  ``missed_window`` the agent was down over the ping time, so you were never
                    warned. Recorded to keep the gap visible in the data, and
                    excluded from advice — it measures the server, not you.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import alerts
from .config import Config
from .ingest.timeutil import parse_iso
from .predict import first_event_between

ON_TIME = "on_time"
LATE = "late"
SKIPPED = "skipped"
NO_SHOW = "no_show"
MISSED_WINDOW = "missed_window"

#: Outcomes that describe your own timing, and so may inform advice.
TIMING_OUTCOMES = (ON_TIME, LATE)


@dataclass
class Grade:
    alert_id: int
    outcome: str
    actual_depart_utc: datetime | None
    actual_arrive_utc: datetime | None
    #: arrive_by - actual_arrival. Positive = spare time; negative = late.
    slack_sec: int | None
    #: actual_departure - planned_departure. Positive = you dawdled.
    depart_delta_sec: int | None


def grade_alert(
    conn: sqlite3.Connection, config: Config, row: sqlite3.Row, now: datetime
) -> Grade | None:
    """Grade one sent alert, or None if it's too early to judge."""
    commitment = config.commitment(row["commitment_id"])
    if commitment is None:
        return None  # commitment deleted from config; leave the row alone

    arrive_by = parse_iso(row["arrive_by_utc"])
    planned_depart = parse_iso(row["planned_depart_utc"])
    settle = timedelta(seconds=config.max_transit_sec)
    if now < arrive_by + settle:
        return None

    # Search for the arrival generously: from the ping until well after the
    # deadline, so a late arrival is *found and scored late* rather than lost.
    search_from = planned_depart - timedelta(seconds=commitment.prep_sec)
    arrival = first_event_between(
        conn, commitment.destination, "enter", search_from, arrive_by + settle
    )
    departure = first_event_between(
        conn,
        commitment.origin,
        "exit",
        search_from,
        arrival or (arrive_by + settle),
    )

    depart_delta = (
        int((departure - planned_depart).total_seconds()) if departure else None
    )

    if arrival is None:
        return Grade(row["id"], NO_SHOW, departure, None, None, depart_delta)

    slack = int((arrive_by - arrival).total_seconds())
    outcome = ON_TIME if slack >= -config.alerting.on_time_grace_sec else LATE
    return Grade(row["id"], outcome, departure, arrival, slack, depart_delta)


def grade_pending(
    conn: sqlite3.Connection, config: Config, now: datetime | None = None
) -> list[Grade]:
    """Grade every alert that's ripe. Safe to call on every tick."""
    now = now or datetime.now(timezone.utc)
    out: list[Grade] = []
    for row in alerts.ungraded(conn):
        g = grade_alert(conn, config, row, now)
        if g is None:
            continue
        alerts.set_outcome(
            conn, g.alert_id, g.outcome, g.actual_depart_utc, g.actual_arrive_utc, now
        )
        out.append(g)
    return out

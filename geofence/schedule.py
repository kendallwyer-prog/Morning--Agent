"""Turning "swim practice at 6:00 on weekdays" into "ping me at 05:37 UTC-5".

Two responsibilities:

1. **Calendar maths**, DST-safe. ``arrive_by`` is a *local wall clock* time, so
   it is resolved against the configured zone on each occurrence date rather
   than stored as a fixed UTC offset. The morning the clocks change, 6:00am
   practice is still 6:00am.
2. **Departure maths**::

       must_leave = arrive_by - travel_estimate - safety_margin_sec
       ping_at    = must_leave - prep_sec

   ``prep_sec`` is your shoes-and-goggles time: the ping lands that long before
   you have to be out the door, so "on my way" is a plan, not a panic.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Commitment, Config, _parse_hhmm
from .predict import TravelEstimate, estimate_travel


@dataclass
class Occurrence:
    """One commitment on one specific day, fully resolved."""

    commitment: Commitment
    occurrence_date: str      # LOCAL date, YYYY-MM-DD (the alerts table key)
    arrive_by_utc: datetime
    must_leave_utc: datetime
    ping_at_utc: datetime
    estimate: TravelEstimate

    @property
    def id(self) -> str:
        return self.commitment.id


def local_datetime(day: date, hhmm: str, tz_name: str) -> datetime:
    """Local wall-clock time on `day`, as a tz-aware UTC datetime.

    Resolved through ``zoneinfo`` per-date, so DST is handled by construction.
    On the spring-forward morning a nonexistent local time (02:00-02:59) is
    normalized by the zone rather than raising -- no commitment here lands in
    that hour, and silently shifting beats crashing the scheduler at 2am.
    """
    hh, mm = _parse_hhmm(hhmm)
    local = datetime(day.year, day.month, day.day, hh, mm, tzinfo=ZoneInfo(tz_name))
    return local.astimezone(ZoneInfo("UTC"))


def today_local(config: Config, now: datetime) -> date:
    return now.astimezone(ZoneInfo(config.timezone)).date()


def occurrences_for_date(
    conn: sqlite3.Connection, config: Config, day: date, now: datetime | None = None
) -> list[Occurrence]:
    """Every commitment that runs on `day`, with its departure times computed
    from the current travel estimates. Sorted by when you must leave."""
    out: list[Occurrence] = []
    for c in config.commitments:
        if not c.runs_on(day.weekday()):
            continue
        arrive_by = local_datetime(day, c.arrive_by, config.timezone)
        est = estimate_travel(
            conn, config, c.origin, c.destination, c.fallback_travel_sec, now
        )
        must_leave = arrive_by - timedelta(seconds=est.seconds + c.safety_margin_sec)
        out.append(
            Occurrence(
                commitment=c,
                occurrence_date=day.isoformat(),
                arrive_by_utc=arrive_by,
                must_leave_utc=must_leave,
                ping_at_utc=must_leave - timedelta(seconds=c.prep_sec),
                estimate=est,
            )
        )
    out.sort(key=lambda o: o.must_leave_utc)
    return out


def upcoming(
    conn: sqlite3.Connection, config: Config, now: datetime, days: int = 2
) -> list[Occurrence]:
    """Occurrences from today forward. Spans midnight so a 6am practice is
    already planned the evening before."""
    start = today_local(config, now)
    out: list[Occurrence] = []
    for i in range(days):
        out.extend(occurrences_for_date(conn, config, start + timedelta(days=i), now))
    out.sort(key=lambda o: o.must_leave_utc)
    return out


def due_now(
    conn: sqlite3.Connection, config: Config, now: datetime
) -> list[Occurrence]:
    """Occurrences whose ping time has arrived.

    The ``max_late_send_sec`` guard is the important half: if the agent was
    down over a departure time, an alert that fires hours later is worse than
    no alert -- it would tell you to leave for something you have already
    missed. Those are recorded as ``missed_window`` by the caller instead.
    """
    window = timedelta(seconds=config.alerting.max_late_send_sec)
    return [
        o
        for o in upcoming(conn, config, now, days=2)
        if o.ping_at_utc <= now <= o.ping_at_utc + window
    ]


def stale(
    conn: sqlite3.Connection, config: Config, now: datetime
) -> list[Occurrence]:
    """Occurrences whose ping window has passed unsent (agent was down)."""
    window = timedelta(seconds=config.alerting.max_late_send_sec)
    return [
        o
        for o in upcoming(conn, config, now, days=2)
        if now > o.ping_at_utc + window and now < o.arrive_by_utc + timedelta(hours=6)
    ]

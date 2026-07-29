"""`doctor`: detect the silent failure that is most likely to kill this project.

The whole premise is automatic capture. If events quietly stop arriving -- a
Shortcut got toggled off, OwnTracks lost background permission, the phone died,
the server's clock drifted -- you'd never know until your data had a hole in it.
`doctor` makes that loud: it reports the age of the most recent event and exits
non-zero when nothing has arrived within the configured silence threshold.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import Config
from .ingest.timeutil import parse_iso


@dataclass
class DoctorReport:
    ok: bool
    now_utc: datetime
    last_event_utc: datetime | None
    silence_hours: float
    age_hours: float | None
    total_raw: int
    events_24h: int
    incomplete_segments: int
    suspect_segments: int
    per_region_last: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def run_doctor(conn: sqlite3.Connection, config: Config, now: datetime | None = None) -> DoctorReport:
    now = now or datetime.now(timezone.utc)
    problems: list[str] = []

    # MAX(), not "last row by id": normally receipt order IS insertion order,
    # but a backfilled or replayed payload would otherwise make the newest data
    # look ancient and fire a false silence alarm.
    last_row = conn.execute(
        "SELECT MAX(received_at_utc) AS received_at_utc FROM raw_events"
    ).fetchone()
    if last_row and last_row["received_at_utc"] is None:
        last_row = None
    last_event = parse_iso(last_row["received_at_utc"]) if last_row else None

    age_hours = None
    if last_event is None:
        problems.append("No events have EVER been received.")
    else:
        age_hours = (now - last_event).total_seconds() / 3600.0
        if age_hours > config.silence_threshold_hours:
            problems.append(
                f"SILENT: last event was {age_hours:.1f}h ago "
                f"(threshold {config.silence_threshold_hours}h). "
                f"Check the Shortcut/OwnTracks is still running."
            )

    total_raw = conn.execute("SELECT COUNT(*) c FROM raw_events").fetchone()["c"]
    day_ago = (now.timestamp() - 86400)
    events_24h = 0
    for r in conn.execute("SELECT received_at_utc FROM raw_events").fetchall():
        try:
            if parse_iso(r["received_at_utc"]).timestamp() >= day_ago:
                events_24h += 1
        except Exception:
            pass

    incomplete = conn.execute(
        "SELECT COUNT(*) c FROM segments WHERE status = 'incomplete'"
    ).fetchone()["c"]
    suspect = conn.execute(
        "SELECT COUNT(*) c FROM segments WHERE status = 'suspect'"
    ).fetchone()["c"]

    per_region: dict[str, str] = {}
    for rid in config.regions:
        row = conn.execute(
            "SELECT event_utc FROM events WHERE region = ? ORDER BY event_utc DESC LIMIT 1",
            (rid,),
        ).fetchone()
        per_region[rid] = row["event_utc"] if row else "never"

    return DoctorReport(
        ok=not problems,
        now_utc=now,
        last_event_utc=last_event,
        silence_hours=config.silence_threshold_hours,
        age_hours=age_hours,
        total_raw=total_raw,
        events_24h=events_24h,
        incomplete_segments=incomplete,
        suspect_segments=suspect,
        per_region_last=per_region,
        problems=problems,
    )


def format_report(rep: DoctorReport) -> str:
    lines = []
    status = "OK" if rep.ok else "PROBLEM"
    lines.append(f"doctor: {status}")
    if rep.last_event_utc:
        lines.append(
            f"  last event:  {rep.last_event_utc.isoformat()} "
            f"({rep.age_hours:.1f}h ago)"
        )
    else:
        lines.append("  last event:  none")
    lines.append(f"  raw total:   {rep.total_raw}   (last 24h: {rep.events_24h})")
    lines.append(
        f"  segments:    {rep.incomplete_segments} incomplete, "
        f"{rep.suspect_segments} suspect"
    )
    lines.append("  last seen per region:")
    for rid, when in rep.per_region_last.items():
        lines.append(f"    {rid:<18} {when}")
    for p in rep.problems:
        lines.append(f"  ! {p}")
    return "\n".join(lines)

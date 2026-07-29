"""«/coffee» — when can I get coffee without wrecking the rest of the day?

You ask; the agent answers with a real window, not a vague yes. It plans the
whole round trip against the same measured travel times the departure alerts
use:

    out (where you are → the coffee shop)
  + time in the shop (queue + collect)
  + back (shop → wherever you need to be next)
  + slack
  ≤ the gap before your next must-leave time

Candidate windows are every gap in today's remaining schedule:

* **Right now**, starting from wherever you currently are.
* **After each commitment**, starting from that commitment's destination once
  it's plausibly over. "Plausibly over" is your own median dwell at that place
  — measured, not assumed, so the window after practice reflects how long you
  actually spend at DeNunzio rather than how long practice is nominally listed.

The earliest feasible window wins, since coffee you're told about now beats
coffee two hours from now. Each answer names the commitment that bounds it, so
the constraint is visible rather than mysterious.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Config, _parse_hhmm
from .predict import current_region, estimate_dwell, estimate_travel
from .schedule import Occurrence, local_datetime, today_local, upcoming

#: Assumed dwell when a commitment's destination has no dwell history yet.
_DEFAULT_COMMITMENT_DWELL_SEC = 3600


@dataclass
class CoffeeWindow:
    """A feasible (or rejected) coffee opportunity."""

    start_utc: datetime
    #: Latest you could set off and still make the next commitment.
    latest_start_utc: datetime
    from_region: str
    out_sec: int
    back_sec: int
    #: The commitment this window butts up against, if any.
    bounded_by: Occurrence | None
    #: Set when the window was considered and rejected.
    reason: str | None = None

    @property
    def feasible(self) -> bool:
        return self.reason is None

    @property
    def round_trip_sec(self) -> int:
        return self.out_sec + self.back_sec


def _fmt_local(dt: datetime, tz: str) -> str:
    return dt.astimezone(ZoneInfo(tz)).strftime("%-I:%M %p")


def _day_bounds(config: Config, now: datetime) -> tuple[datetime, datetime]:
    day = today_local(config, now)
    return (
        local_datetime(day, config.coffee.earliest, config.timezone),
        local_datetime(day, config.coffee.latest, config.timezone),
    )


def _window(
    conn: sqlite3.Connection,
    config: Config,
    start: datetime,
    from_region: str,
    next_occ: Occurrence | None,
    day_end: datetime,
) -> CoffeeWindow:
    """Evaluate one candidate window: leave `from_region` at `start`."""
    cf = config.coffee
    out = estimate_travel(conn, config, from_region, cf.region, cf.fallback_travel_sec, start)
    # Return leg: back to wherever you must be next. With nothing left today
    # there's no return constraint, so the trip back costs nothing schedule-wise.
    if next_occ is not None:
        back = estimate_travel(
            conn, config, cf.region, next_occ.commitment.origin, cf.fallback_travel_sec, start
        )
        back_sec = back.seconds
        deadline = next_occ.must_leave_utc
    else:
        back_sec = 0
        deadline = day_end

    needed = timedelta(seconds=out.seconds + cf.dwell_sec + back_sec + cf.slack_sec)
    latest_start = deadline - needed

    win = CoffeeWindow(
        start_utc=start,
        latest_start_utc=latest_start,
        from_region=from_region,
        out_sec=out.seconds,
        back_sec=back_sec,
        bounded_by=next_occ,
    )
    if start > day_end:
        win.reason = "outside coffee hours"
    elif latest_start < start:
        short = int((start - latest_start).total_seconds())
        win.reason = f"{short // 60} min too tight"
    return win


def find_windows(
    conn: sqlite3.Connection, config: Config, now: datetime
) -> list[CoffeeWindow]:
    """Every candidate window for the rest of today, in chronological order."""
    day_start, day_end = _day_bounds(config, now)
    today = today_local(config, now).isoformat()
    remaining = [
        o
        for o in upcoming(conn, config, now, days=1)
        if o.occurrence_date == today and o.must_leave_utc > now
    ]

    here = current_region(conn) or (
        remaining[0].commitment.origin if remaining else config.coffee.region
    )
    windows: list[CoffeeWindow] = []

    # Window 0: right now, from wherever you are.
    windows.append(
        _window(
            conn, config, max(now, day_start), here,
            remaining[0] if remaining else None, day_end,
        )
    )

    # One window after each remaining commitment, starting from its destination
    # once your typical stay there is over.
    for i, occ in enumerate(remaining):
        dest = occ.commitment.destination
        stay = estimate_dwell(conn, config, dest, _DEFAULT_COMMITMENT_DWELL_SEC, now)
        start = occ.arrive_by_utc + timedelta(seconds=stay.seconds)
        nxt = remaining[i + 1] if i + 1 < len(remaining) else None
        windows.append(_window(conn, config, max(start, day_start), dest, nxt, day_end))

    return windows


def best_window(
    conn: sqlite3.Connection, config: Config, now: datetime
) -> CoffeeWindow | None:
    """The earliest feasible window, or None if today has no room."""
    for w in find_windows(conn, config, now):
        if w.feasible:
            return w
    return None


def format_suggestion(
    conn: sqlite3.Connection, config: Config, now: datetime
) -> str:
    """The Telegram reply to /coffee."""
    tz = config.timezone
    windows = find_windows(conn, config, now)
    best = next((w for w in windows if w.feasible), None)

    if best is None:
        lines = ["☕ No room for coffee today without cutting something fine."]
        for w in windows:
            where = config.region_name(w.from_region)
            if w.bounded_by:
                lines.append(
                    f"  • from {where}: {w.reason} "
                    f"before {w.bounded_by.commitment.label}"
                )
            else:
                lines.append(f"  • from {where}: {w.reason}")
        return "\n".join(lines)

    now_ish = best.start_utc <= now + timedelta(minutes=2)
    when = "now" if now_ish else _fmt_local(best.start_utc, tz)
    lines = [
        f"☕ Best time: <b>{when}</b>"
        + ("" if now_ish else f" (from {config.region_name(best.from_region)})"),
        f"   Round trip {best.round_trip_sec // 60} min "
        f"+ {config.coffee.dwell_sec // 60} min in the shop.",
    ]
    if best.bounded_by is not None:
        lines.append(
            f"   Set off by {_fmt_local(best.latest_start_utc, tz)} to still make "
            f"{best.bounded_by.commitment.label}."
        )
    else:
        lines.append("   Nothing else scheduled today — no rush.")
    return "\n".join(lines)

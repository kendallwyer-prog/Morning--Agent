"""The weekly summary: what your week actually looked like.

You asked to be told when to leave earlier or later. ``advice`` answers that on
demand, but the useful version arrives without being asked — once a week, with
the numbers behind it.

The digest reports, per commitment: how long the leg really takes you now, your
on-time record, and any suggested change to the two preference knobs. It also
surfaces data-quality problems (incomplete pairs, missed windows), because a
week where a third of your trips didn't record is a week whose numbers you
shouldn't act on.

Sent at most once per week, and keyed on **the most recent send moment that has
passed** rather than on a timestamp or a calendar week. That one choice gets
all three cases right:

* Sunday 19:00 arrives → sent.
* Ticked again at 19:00:20, or restarted at 21:00 → same key, not resent.
* The box was off all Sunday evening and boots Monday → the key is still
  Sunday's, so you get that week's digest late rather than never. And only
  that one: a machine off for three weeks sends a single current digest, not a
  backlog.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import alerts
from .advice import advise_commitment
from .config import Config, _DAY_IDS, _parse_hhmm
from .grading import LATE, MISSED_WINDOW, NO_SHOW, ON_TIME, SKIPPED
from .ingest.timeutil import iso_utc
from .notify.telegram import TelegramClient
from .predict import estimate_travel, observed_durations
from .stats import _median

_LAST_PERIOD_KEY = "digest_last_period"


def _fmt_mins(sec: float | None) -> str:
    if sec is None:
        return "—"
    return f"{sec / 60:.0f} min"


def _counts(conn: sqlite3.Connection, commitment_id: str, since: datetime) -> dict:
    rows = conn.execute(
        "SELECT outcome, COUNT(*) c FROM alerts "
        "WHERE commitment_id = ? AND kind = 'departure' AND arrive_by_utc >= ? "
        "GROUP BY outcome",
        (commitment_id, iso_utc(since)),
    ).fetchall()
    return {r["outcome"]: r["c"] for r in rows if r["outcome"]}


def build(
    conn: sqlite3.Connection, config: Config, now: datetime | None = None
) -> str:
    """Render the digest for the week ending `now`."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=7)
    lines = ["<b>📊 Your week</b>"]

    any_data = False
    for c in config.commitments:
        counts = _counts(conn, c.id, since)
        done = counts.get(ON_TIME, 0) + counts.get(LATE, 0)
        skipped = counts.get(SKIPPED, 0)
        missed = counts.get(MISSED_WINDOW, 0)
        no_show = counts.get(NO_SHOW, 0)

        lines.append(f"\n<b>{c.label}</b>")
        if done:
            any_data = True
            lines.append(
                f"  On time {counts.get(ON_TIME, 0)}/{done}"
                + (f", skipped {skipped}" if skipped else "")
                + (f", no-show {no_show}" if no_show else "")
            )
        else:
            lines.append(
                "  No completed trips this week"
                + (f" ({skipped} skipped)" if skipped else "")
            )

        # What the leg actually costs you now, median vs the p80 the alerts use.
        vals = observed_durations(
            conn, c.origin, c.destination, config.alerting.lookback_days, now
        )
        if vals:
            any_data = True
            est = estimate_travel(
                conn, config, c.origin, c.destination, c.fallback_travel_sec, now
            )
            lines.append(
                f"  Walk: typically {_fmt_mins(_median(vals))}, "
                f"planned on {_fmt_mins(est.seconds)} "
                f"(p{config.alerting.percentile:.0f} of {len(vals)} trips)"
            )

        adv = advise_commitment(conn, config, c)
        for line in adv.lines:
            lines.append(f"  {line}")
        for key, cur, new in adv.suggestions:
            lines.append(f"  → geofence.toml: <code>{key}</code> {cur} → {new}")
        if missed:
            lines.append(f"  ⚠️ {missed} alert(s) missed — the agent was down.")

    # Data quality: numbers you shouldn't act on should say so.
    broken = conn.execute(
        "SELECT COUNT(*) c FROM segments WHERE status != 'complete' AND start_utc >= ?",
        (iso_utc(since),),
    ).fetchone()["c"]
    total = conn.execute(
        "SELECT COUNT(*) c FROM segments WHERE start_utc >= ?", (iso_utc(since),)
    ).fetchone()["c"]
    if total and broken / total > 0.3:
        lines.append(
            f"\n⚠️ {broken} of {total} segments this week were incomplete or "
            f"suspect. Estimates are thinner than they look — worth checking "
            f"the geofence radii."
        )

    if not any_data:
        lines.append("\nNot much data yet. Give it a week or two of walking.")
    return "\n".join(lines)


def last_send_moment(config: Config, now: datetime) -> str:
    """The most recent local (day, time) that has already passed, as a date
    string. This is the digest's period key -- see the module docstring."""
    local = now.astimezone(ZoneInfo(config.timezone))
    hh, mm = _parse_hhmm(config.digest.time)
    target = _DAY_IDS.index(config.digest.day)

    back = (local.weekday() - target) % 7
    moment = (local - timedelta(days=back)).replace(
        hour=hh, minute=mm, second=0, microsecond=0
    )
    if moment > local:            # today IS the day, but the time hasn't come
        moment -= timedelta(days=7)
    return moment.date().isoformat()


def maybe_send(
    conn: sqlite3.Connection,
    config: Config,
    client: TelegramClient,
    now: datetime | None = None,
) -> bool:
    """Send this week's digest if it's due and unsent. Returns whether it went."""
    now = now or datetime.now(timezone.utc)
    if not config.digest.enabled or not config.commitments:
        return False

    period = last_send_moment(config, now)
    seen = alerts.state_get(conn, _LAST_PERIOD_KEY)
    if seen == period:
        return False
    if seen is None:
        # First ever run: adopt the current period silently instead of firing a
        # digest at whatever moment the agent happened to be installed.
        alerts.state_set(conn, _LAST_PERIOD_KEY, period)
        return False

    if not client.send(build(conn, config, now)).ok:
        # Don't mark it sent if it didn't go -- retry on the next tick.
        return False
    alerts.state_set(conn, _LAST_PERIOD_KEY, period)
    return True

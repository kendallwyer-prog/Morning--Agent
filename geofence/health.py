"""The silent failure that would actually kill this project, made loud.

Everything here rests on geofence crossings arriving by themselves. If they
stop — a Shortcut toggled off after an iOS update, OwnTracks losing background
location permission, a dead phone, a server whose clock drifted — nothing
visibly breaks. Alerts keep firing on stale estimates, every occurrence quietly
grades as ``no_show``, and you don't find out until you look at a graph weeks
later and see a hole.

Phase 1's ``doctor`` exits non-zero for cron to catch. That's the right tool
for a machine, but you now have a channel that reaches *you*, so the warning
goes to Telegram too.

Two rules keep it from becoming noise:

* **Repeat, don't spam.** Once told, you aren't told again for
  ``silence_repeat_hours`` (default 12). A warning that arrives every 20
  seconds is one you learn to swipe away, which is worse than no warning.
* **Say when it's fixed.** The recovery message is what makes the warning
  trustworthy: you learn that silence from the bot genuinely means the data is
  flowing, rather than that it gave up.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from . import alerts
from .config import Config
from .doctor import run_doctor
from .notify.telegram import TelegramClient

#: bot_state keys. `state` is 'ok' | 'silent'; `warned_at` throttles repeats.
_STATE_KEY = "health_state"
_WARNED_AT_KEY = "health_warned_at"


@dataclass
class HealthAction:
    """What the watch decided to do this tick (None = stay quiet)."""

    kind: str | None      # 'warned' | 'recovered' | None
    message: str | None = None


def _hours_since(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    from .ingest.timeutil import parse_iso

    return (now - parse_iso(iso)).total_seconds() / 3600.0


def check(
    conn: sqlite3.Connection,
    config: Config,
    client: TelegramClient | None,
    now: datetime | None = None,
) -> HealthAction:
    """Warn on silence, and say so when it clears. Safe to call every tick."""
    now = now or datetime.now(timezone.utc)
    rep = run_doctor(conn, config, now)
    previous = alerts.state_get(conn, _STATE_KEY) or "ok"

    # `doctor` also flags "no events EVER", which on a fresh install is just
    # "you haven't set the Shortcut up yet" -- not a regression worth paging
    # about. Only an established stream going quiet is an incident.
    silent = rep.age_hours is not None and rep.age_hours > config.silence_threshold_hours

    if silent:
        last_warn = _hours_since(alerts.state_get(conn, _WARNED_AT_KEY), now)
        if previous == "silent" and last_warn is not None:
            if last_warn < config.alerting.silence_repeat_hours:
                return HealthAction(None)

        seen = [(rid, t) for rid, t in rep.per_region_last.items() if t != "never"]
        last_place = (
            config.region_name(max(seen, key=lambda kv: kv[1])[0]) if seen else "nowhere"
        )
        msg = (
            f"⚠️ <b>No geofence data for {rep.age_hours:.0f}h.</b>\n"
            f"Departure times are running on stale estimates, and today's "
            f"commitments will grade as no-shows.\n"
            f"Check the Shortcut automations are still set to <b>Run "
            f"Immediately</b> (iOS turns them off after some updates), and that "
            f"Location access is still <b>Always</b>.\n"
            f"Last seen at: {last_place}."
        )
        if client is not None:
            result = client.send(msg)
            if not result.ok:
                # Couldn't tell you -- don't record that we did, so the next
                # tick tries again rather than silently swallowing the warning.
                return HealthAction(None)
        alerts.state_set(conn, _STATE_KEY, "silent")
        alerts.state_set(conn, _WARNED_AT_KEY, now.isoformat())
        return HealthAction("warned", msg)

    if previous == "silent":
        n = rep.events_24h
        msg = (
            f"✅ <b>Geofence data is flowing again</b> "
            f"({n} event{'' if n == 1 else 's'} in the last 24h)."
        )
        if client is not None and not client.send(msg).ok:
            return HealthAction(None)
        alerts.state_set(conn, _STATE_KEY, "ok")
        return HealthAction("recovered", msg)

    return HealthAction(None)

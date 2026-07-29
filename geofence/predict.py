"""How long does this leg actually take me?

Everything the alerting layer knows about your pace comes from here. The design
choices worth arguing about:

* **A percentile, not a mean.** You do not care about your typical walk to the
  pool; you care about not being late. The default is p80, so roughly one trip
  in five runs longer than the estimate -- and the per-commitment
  ``safety_margin_sec`` covers that tail. Tune ``[alerting] percentile`` up if
  you want to be later-proof at the cost of leaving earlier.
* **Recent data only.** A ``lookback_days`` window means an estimate tracks how
  you actually move *now* -- a snowy February or a knee injury shows up in the
  advice within a week or two instead of being diluted by all of history.
* **Cold start is explicit, never silent.** Under ``min_samples`` observations
  the estimate falls back to the commitment's configured guess and reports
  ``source='fallback'``, which the Telegram message shows verbatim. A
  confident-looking "7 min" derived from two data points would be a lie.
* **Only ``complete`` segments count.** Incomplete (broken pair -- dead phone,
  got a ride) and suspect (overlapping-geofence flip) segments are excluded by
  the same rule the ``stats`` command uses, so one bad day can't shift a
  departure time.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import Config
from .ingest.timeutil import iso_utc, parse_iso
from .stats import _percentile


@dataclass
class TravelEstimate:
    """An answer to "how long will this leg take?" plus its provenance, so the
    caller can be honest with the user about how much to trust it."""

    seconds: int
    source: str        # 'observed' | 'fallback'
    n: int             # number of complete segments behind an observed estimate
    origin: str
    destination: str

    @property
    def confident(self) -> bool:
        return self.source == "observed"

    def describe(self) -> str:
        mins = self.seconds / 60.0
        if self.confident:
            return f"~{mins:.0f} min (from {self.n} trips)"
        return f"~{mins:.0f} min (estimate — not enough data yet)"


def observed_durations(
    conn: sqlite3.Connection,
    origin: str,
    destination: str,
    lookback_days: int,
    now: datetime | None = None,
) -> list[int]:
    """Complete transit durations for one leg, within the lookback window."""
    now = now or datetime.now(timezone.utc)
    since = iso_utc(now - timedelta(days=lookback_days))
    rows = conn.execute(
        "SELECT duration_sec FROM segments "
        "WHERE segment_type = 'transit' AND status = 'complete' "
        "  AND duration_sec IS NOT NULL "
        "  AND from_region = ? AND to_region = ? AND start_utc >= ?",
        (origin, destination, since),
    ).fetchall()
    return sorted(r["duration_sec"] for r in rows)


def estimate_travel(
    conn: sqlite3.Connection,
    config: Config,
    origin: str,
    destination: str,
    fallback_sec: int,
    now: datetime | None = None,
) -> TravelEstimate:
    """Percentile travel time for a leg, or the configured fallback."""
    a = config.alerting
    vals = observed_durations(conn, origin, destination, a.lookback_days, now)
    if len(vals) < a.min_samples:
        return TravelEstimate(
            seconds=int(fallback_sec),
            source="fallback",
            n=len(vals),
            origin=origin,
            destination=destination,
        )
    return TravelEstimate(
        seconds=int(round(_percentile(vals, a.percentile))),
        source="observed",
        n=len(vals),
        origin=origin,
        destination=destination,
    )


def estimate_dwell(
    conn: sqlite3.Connection,
    config: Config,
    region: str,
    fallback_sec: int,
    now: datetime | None = None,
) -> TravelEstimate:
    """How long you typically *stay* somewhere.

    Unlike travel, this uses the **median**, not p80: the coffee planner needs
    the realistic length of practice, and a pessimistic p80 would wrongly rule
    out otherwise perfectly good windows afterwards.
    """
    now = now or datetime.now(timezone.utc)
    a = config.alerting
    since = iso_utc(now - timedelta(days=a.lookback_days))
    rows = conn.execute(
        "SELECT duration_sec FROM segments "
        "WHERE segment_type = 'dwell' AND status = 'complete' "
        "  AND duration_sec IS NOT NULL AND from_region = ? AND start_utc >= ?",
        (region, since),
    ).fetchall()
    vals = sorted(r["duration_sec"] for r in rows)
    if len(vals) < a.min_samples:
        return TravelEstimate(int(fallback_sec), "fallback", len(vals), region, region)
    return TravelEstimate(
        int(round(_percentile(vals, 50.0))), "observed", len(vals), region, region
    )


def current_region(conn: sqlite3.Connection) -> str | None:
    """Where you are right now: the region you most recently entered and have
    not exited. Returns None when the last event was an exit (you're in transit
    or somewhere with no geofence) or when there are no events at all.

    Suppressed (flap) events are ignored, so standing on a boundary doesn't
    make your location oscillate.
    """
    row = conn.execute(
        "SELECT region, transition FROM events WHERE suppressed = 0 "
        "ORDER BY event_utc DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None or row["transition"] != "enter":
        return None
    return row["region"]


def first_event_between(
    conn: sqlite3.Connection,
    region: str,
    transition: str,
    start_utc: datetime,
    end_utc: datetime,
) -> datetime | None:
    """First non-suppressed `transition` at `region` in a time window.

    Used by grading to find when you *actually* left and *actually* arrived.
    Uses the calibrated time so the known-late iOS "leave" bias is corrected.
    """
    row = conn.execute(
        "SELECT calibrated_utc FROM events "
        "WHERE suppressed = 0 AND region = ? AND transition = ? "
        "  AND calibrated_utc >= ? AND calibrated_utc <= ? "
        "ORDER BY calibrated_utc LIMIT 1",
        (region, transition, iso_utc(start_utc), iso_utc(end_utc)),
    ).fetchone()
    return parse_iso(row["calibrated_utc"]) if row else None

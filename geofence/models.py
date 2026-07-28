"""Internal data shapes. Both client formats normalize into ``NormalizedEvent``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class NormalizedEvent:
    """One geofence crossing, normalized from either client into one shape."""

    client_type: str          # 'shortcuts' | 'owntracks'
    dedupe_key: str           # client UUID, or synthesized (idempotency key)
    region: str               # canonical region id
    transition: str           # 'enter' | 'exit'
    event_utc: datetime       # device event time, UTC, tz-aware
    lat: float | None = None
    lon: float | None = None
    acc: float | None = None


@dataclass
class ProcEvent:
    """A lightweight event used by the pure processing functions (debounce,
    segment building). ``ts`` is whichever time the caller wants applied --
    raw event time for debounce, calibrated time for segment durations."""

    id: int
    region: str
    transition: str
    ts: datetime


@dataclass
class Segment:
    segment_type: str              # 'transit' (A->B) | 'dwell' (in A)
    from_region: str
    to_region: str | None          # None when incomplete transit
    start_event_id: int
    end_event_id: int | None       # None when incomplete
    start_utc: datetime
    end_utc: datetime | None       # None when incomplete
    duration_sec: int | None       # None when incomplete
    status: str                    # 'complete' | 'incomplete' | 'suspect'
    status_reason: str | None = None

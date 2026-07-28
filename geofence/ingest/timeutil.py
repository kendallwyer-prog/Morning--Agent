"""UTC <-> local time, DST-safe, via the stdlib ``zoneinfo``.

We store BOTH a UTC timestamp and a local wall-clock string, plus the UTC
offset in minutes at that instant. Storing the offset means DST is captured
explicitly per event -- a segment that straddles the spring-forward / fall-back
boundary still has correct real-elapsed durations (durations are always
computed from the UTC side).
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def parse_utc(value) -> datetime:
    """Parse an epoch (int/float/str) or ISO-8601 string into a tz-aware UTC
    datetime. A naive ISO string is assumed to already be UTC."""
    if isinstance(value, bool):  # guard: bool is an int subclass
        raise ValueError(f"invalid timestamp: {value!r}")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            raise ValueError("empty timestamp")
        # bare epoch seconds as a string
        try:
            return datetime.fromtimestamp(float(s), tz=timezone.utc)
        except (ValueError, OSError):
            pass
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    raise ValueError(f"unparseable timestamp: {value!r}")


def to_local(dt_utc: datetime, tz_name: str) -> tuple[datetime, int]:
    """Return (local tz-aware datetime, utc offset in minutes at that instant)."""
    local = dt_utc.astimezone(ZoneInfo(tz_name))
    offset = local.utcoffset()
    offset_min = int(offset.total_seconds() // 60) if offset else 0
    return local, offset_min


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(s: str) -> datetime:
    """Parse a stored ISO-8601 string back into a tz-aware UTC datetime."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

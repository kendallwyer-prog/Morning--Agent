"""Boundary-flap suppression.

Standing near a geofence edge produces enter/exit/enter/exit noise. The rule
(spec problem #1): discard an exit that lands within ``flap_window_sec`` of an
enter for the SAME region. The threshold is a config value, not a magic number.

Debounce runs on the *raw recorded* event times (calibration is applied later,
only to the events that survive).
"""

from __future__ import annotations

from ..models import ProcEvent


def compute_suppressed(events: list[ProcEvent], flap_window_sec: int) -> list[int]:
    """Return the ids of exit events to suppress as boundary flaps.

    An exit for region R is suppressed if the most recent enter for R happened
    no more than ``flap_window_sec`` seconds earlier. The enter is kept (you
    entered and stayed); the flapping exit is noise.
    """
    ordered = sorted(events, key=lambda e: (e.ts, e.id))
    last_enter: dict[str, object] = {}
    suppressed: list[int] = []

    for e in ordered:
        if e.transition == "enter":
            last_enter[e.region] = e.ts
        elif e.transition == "exit":
            t = last_enter.get(e.region)
            if t is not None and (e.ts - t).total_seconds() <= flap_window_sec:
                # Flap: discard this exit. Keep last_enter so that a later,
                # genuine exit of the same region can still be recognised.
                suppressed.append(e.id)
            else:
                # Genuine exit: consume the pending enter so a subsequent stray
                # exit isn't compared against a stale enter.
                last_enter.pop(e.region, None)

    return suppressed

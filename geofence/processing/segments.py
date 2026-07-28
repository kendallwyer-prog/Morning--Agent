"""Segment matching.

Two segment types (you asked for both):

  transit -- exit-from-A paired with the next enter-at-B. duration = t(enter B)
             - t(exit A). This is "how long the trip between two places took".
  dwell   -- enter-at-A paired with the next exit-at-A. duration = t(exit A) -
             t(enter A). This is "how long I stayed somewhere".

Times passed in are already CALIBRATED (per-region offset applied by the
caller), so durations reflect the bias correction.

Three known problems are handled here:

  * Broken pairs (spec #2): an exit with no plausible arrival within
    ``max_transit_sec`` becomes an ``incomplete`` transit (to_region NULL,
    duration NULL). Incomplete segments are EXCLUDED from stats, so a dead
    phone / a day you got a ride can never be counted as a four-hour walk.
  * Overlapping geofences (spec, henry_hall/firestone): a transit faster than
    the configured per-pair ``min_transit`` is flagged ``suspect`` -- recorded,
    but excluded from stats.

Debounce is assumed to have already run; suppressed events are not passed in.
"""

from __future__ import annotations

from ..models import ProcEvent, Segment


def _incomplete_transit(ex: ProcEvent) -> Segment:
    return Segment(
        segment_type="transit",
        from_region=ex.region,
        to_region=None,
        start_event_id=ex.id,
        end_event_id=None,
        start_utc=ex.ts,
        end_utc=None,
        duration_sec=None,
        status="incomplete",
        status_reason="no_matching_arrival",
    )


def _incomplete_dwell(en: ProcEvent) -> Segment:
    return Segment(
        segment_type="dwell",
        from_region=en.region,
        to_region=en.region,
        start_event_id=en.id,
        end_event_id=None,
        start_utc=en.ts,
        end_utc=None,
        duration_sec=None,
        status="incomplete",
        status_reason="no_matching_exit",
    )


def _build_transit(
    events: list[ProcEvent], max_transit_sec: int, min_transit: dict[str, int]
) -> list[Segment]:
    segs: list[Segment] = []
    last_exit: ProcEvent | None = None

    for e in events:
        if e.transition == "exit":
            if last_exit is not None:
                # Two exits in a row (a missed enter in between): the earlier
                # exit never got an arrival.
                segs.append(_incomplete_transit(last_exit))
            last_exit = e
        else:  # enter
            if last_exit is None:
                continue  # arrival with no pending departure -> no transit
            dur = (e.ts - last_exit.ts).total_seconds()
            if dur < 0 or dur > max_transit_sec:
                # No plausible pairing: arrival too far away (or out of order).
                segs.append(_incomplete_transit(last_exit))
                last_exit = None
                continue
            key = f"{last_exit.region}->{e.region}"
            min_t = min_transit.get(key)
            if min_t is not None and dur < min_t:
                status, reason = "suspect", f"below_min_transit({key})"
            else:
                status, reason = "complete", None
            segs.append(
                Segment(
                    segment_type="transit",
                    from_region=last_exit.region,
                    to_region=e.region,
                    start_event_id=last_exit.id,
                    end_event_id=e.id,
                    start_utc=last_exit.ts,
                    end_utc=e.ts,
                    duration_sec=int(dur),
                    status=status,
                    status_reason=reason,
                )
            )
            last_exit = None

    if last_exit is not None:
        segs.append(_incomplete_transit(last_exit))
    return segs


def _build_dwell(events: list[ProcEvent]) -> list[Segment]:
    segs: list[Segment] = []
    pending: dict[str, ProcEvent] = {}

    for e in events:
        if e.transition == "enter":
            if e.region in pending:
                # Re-enter without an exit: the prior stay never closed.
                segs.append(_incomplete_dwell(pending[e.region]))
            pending[e.region] = e
        else:  # exit
            en = pending.pop(e.region, None)
            if en is None:
                continue  # exit with no matching enter -> can't form a dwell
            dur = (e.ts - en.ts).total_seconds()
            if dur < 0:
                segs.append(_incomplete_dwell(en))
                continue
            segs.append(
                Segment(
                    segment_type="dwell",
                    from_region=en.region,
                    to_region=en.region,
                    start_event_id=en.id,
                    end_event_id=e.id,
                    start_utc=en.ts,
                    end_utc=e.ts,
                    duration_sec=int(dur),
                    status="complete",
                    status_reason=None,
                )
            )

    for en in pending.values():
        segs.append(_incomplete_dwell(en))
    return segs


def build_segments(
    events: list[ProcEvent], max_transit_sec: int, min_transit: dict[str, int] | None = None
) -> list[Segment]:
    """Build all transit + dwell segments from an ordered event stream."""
    min_transit = min_transit or {}
    ordered = sorted(events, key=lambda e: (e.ts, e.id))
    segs = _build_transit(ordered, max_transit_sec, min_transit)
    segs += _build_dwell(ordered)
    segs.sort(key=lambda s: (s.start_utc, s.segment_type))
    return segs

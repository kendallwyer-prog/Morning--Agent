"""Tests for segment matching, esp. the incomplete-pair logic (spec problem #2)
and the overlapping-geofence suspect guard."""

from datetime import datetime, timedelta, timezone

from geofence.models import ProcEvent
from geofence.processing.segments import build_segments

BASE = datetime(2026, 3, 1, 8, 0, 0, tzinfo=timezone.utc)
MAX_TRANSIT = 1800
MIN_TRANSIT = {"henry_hall->firestone": 120, "firestone->henry_hall": 120}


def ev(id, region, transition, offset_sec):
    return ProcEvent(id, region, transition, BASE + timedelta(seconds=offset_sec))


def transits(segs):
    return [s for s in segs if s.segment_type == "transit"]


def dwells(segs):
    return [s for s in segs if s.segment_type == "dwell"]


def test_complete_transit_pair():
    # exit henry_hall, enter denunzio 10 min later.
    events = [ev(1, "henry_hall", "exit", 0), ev(2, "denunzio", "enter", 600)]
    t = transits(build_segments(events, MAX_TRANSIT, MIN_TRANSIT))
    assert len(t) == 1
    assert t[0].status == "complete"
    assert t[0].from_region == "henry_hall"
    assert t[0].to_region == "denunzio"
    assert t[0].duration_sec == 600


def test_exit_with_no_arrival_is_incomplete():
    # Dead phone / got a ride: an exit that never gets a following enter.
    events = [ev(1, "henry_hall", "exit", 0)]
    t = transits(build_segments(events, MAX_TRANSIT, MIN_TRANSIT))
    assert len(t) == 1
    assert t[0].status == "incomplete"
    assert t[0].to_region is None
    assert t[0].duration_sec is None
    assert t[0].status_reason == "no_matching_arrival"


def test_arrival_beyond_max_transit_is_not_a_four_hour_walk():
    # The whole point of problem #2: a 4-hour gap must NOT become a 4h transit.
    events = [ev(1, "henry_hall", "exit", 0), ev(2, "firestone", "enter", 4 * 3600)]
    t = transits(build_segments(events, MAX_TRANSIT, MIN_TRANSIT))
    assert len(t) == 1
    assert t[0].status == "incomplete"
    assert t[0].duration_sec is None
    # No complete transit with a giant duration exists.
    assert all(s.duration_sec != 4 * 3600 for s in t)


def test_incomplete_segments_have_no_duration_to_poison_a_median():
    # A day of broken pairs contributes zero usable durations.
    events = [
        ev(1, "henry_hall", "exit", 0),
        ev(2, "denunzio", "exit", 100),   # two exits in a row -> first incomplete
    ]
    segs = build_segments(events, MAX_TRANSIT, MIN_TRANSIT)
    completes = [s for s in segs if s.status == "complete"]
    assert completes == []


def test_overlapping_geofence_fast_transition_is_suspect():
    # henry_hall <-> firestone overlap: a 30s "trip" is not real.
    events = [ev(1, "henry_hall", "exit", 0), ev(2, "firestone", "enter", 30)]
    t = transits(build_segments(events, MAX_TRANSIT, MIN_TRANSIT))
    assert len(t) == 1
    assert t[0].status == "suspect"
    assert t[0].status_reason == "below_min_transit(henry_hall->firestone)"


def test_slow_transition_between_overlapping_regions_is_complete():
    events = [ev(1, "henry_hall", "exit", 0), ev(2, "firestone", "enter", 300)]
    t = transits(build_segments(events, MAX_TRANSIT, MIN_TRANSIT))
    assert t[0].status == "complete"


def test_complete_dwell_pair():
    events = [ev(1, "firestone", "enter", 0), ev(2, "firestone", "exit", 3600)]
    d = dwells(build_segments(events, MAX_TRANSIT, MIN_TRANSIT))
    assert len(d) == 1
    assert d[0].status == "complete"
    assert d[0].from_region == "firestone"
    assert d[0].duration_sec == 3600


def test_dwell_with_no_exit_is_incomplete():
    events = [ev(1, "firestone", "enter", 0)]
    d = dwells(build_segments(events, MAX_TRANSIT, MIN_TRANSIT))
    assert len(d) == 1
    assert d[0].status == "incomplete"
    assert d[0].status_reason == "no_matching_exit"


def test_full_day_sequence():
    # dorm -> library (dwell) -> pool, with clean pairs throughout.
    events = [
        ev(1, "henry_hall", "exit", 0),
        ev(2, "firestone", "enter", 300),     # transit 5m
        ev(3, "firestone", "exit", 300 + 7200),   # dwell 2h
        ev(4, "denunzio", "enter", 300 + 7200 + 600),  # transit 10m
    ]
    segs = build_segments(events, MAX_TRANSIT, MIN_TRANSIT)
    t = transits(segs)
    d = dwells(segs)
    assert {s.status for s in t} == {"complete"}
    assert len(t) == 2
    # One completed dwell (firestone). The final denunzio enter never exits, so
    # it is an open/incomplete dwell -- correct, and excluded from stats.
    complete_dwells = [s for s in d if s.status == "complete"]
    open_dwells = [s for s in d if s.status == "incomplete"]
    assert len(complete_dwells) == 1
    assert complete_dwells[0].from_region == "firestone"
    assert complete_dwells[0].duration_sec == 7200
    assert len(open_dwells) == 1
    assert open_dwells[0].from_region == "denunzio"

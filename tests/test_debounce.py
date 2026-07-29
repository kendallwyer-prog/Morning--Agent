"""Tests for boundary-flap debounce (spec problem #1)."""

from datetime import datetime, timedelta, timezone

from geofence.models import ProcEvent
from geofence.processing.debounce import compute_suppressed

BASE = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
WINDOW = 90


def ev(id, region, transition, offset_sec):
    return ProcEvent(id, region, transition, BASE + timedelta(seconds=offset_sec))


def test_exit_within_window_is_suppressed():
    events = [ev(1, "firestone", "enter", 0), ev(2, "firestone", "exit", 30)]
    assert compute_suppressed(events, WINDOW) == [2]


def test_exit_at_exactly_window_is_suppressed():
    # "within 90s" is inclusive of the boundary.
    events = [ev(1, "firestone", "enter", 0), ev(2, "firestone", "exit", 90)]
    assert compute_suppressed(events, WINDOW) == [2]


def test_exit_beyond_window_is_kept():
    events = [ev(1, "firestone", "enter", 0), ev(2, "firestone", "exit", 120)]
    assert compute_suppressed(events, WINDOW) == []


def test_classic_flapping_suppresses_every_spurious_exit():
    # enter/exit/enter/exit while standing on the edge -> both exits are noise.
    events = [
        ev(1, "henry_hall", "enter", 0),
        ev(2, "henry_hall", "exit", 20),
        ev(3, "henry_hall", "enter", 40),
        ev(4, "henry_hall", "exit", 55),
    ]
    assert compute_suppressed(events, WINDOW) == [2, 4]


def test_flap_then_genuine_exit_keeps_the_real_one():
    # A flap right after arrival, then hours later the real departure.
    events = [
        ev(1, "firestone", "enter", 0),
        ev(2, "firestone", "exit", 25),      # flap -> suppressed
        ev(3, "firestone", "exit", 3 * 3600),  # real -> kept
    ]
    assert compute_suppressed(events, WINDOW) == [2]


def test_other_region_does_not_affect_debounce():
    # A quick enter/exit at denunzio must not suppress a firestone exit.
    events = [
        ev(1, "firestone", "enter", 0),
        ev(2, "denunzio", "enter", 10),
        ev(3, "denunzio", "exit", 20),       # flap for denunzio
        ev(4, "firestone", "exit", 3600),    # genuine firestone departure
    ]
    assert compute_suppressed(events, WINDOW) == [3]


def test_exit_without_preceding_enter_is_not_suppressed():
    events = [ev(1, "firestone", "exit", 0)]
    assert compute_suppressed(events, WINDOW) == []


def test_threshold_is_configurable():
    events = [ev(1, "firestone", "enter", 0), ev(2, "firestone", "exit", 45)]
    assert compute_suppressed(events, 30) == []   # 45s > 30s window -> kept
    assert compute_suppressed(events, 60) == [2]  # 45s <= 60s window -> flap

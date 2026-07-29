"""Deploy-day check: is every geofence actually reporting both directions?

The bug this exists for is the one that doesn't look like a bug: you build the
"Arrive" automation for a region, forget "Leave", and everything downstream
looks fine — recent events, a healthy doctor, no errors — while that leg
silently never produces a travel time.
"""

from datetime import timedelta

from geofence.config import Region
from geofence.doctor import run_doctor
from geofence.setup_check import run_setup_check

from .conftest import add_event, add_transit, utc


def test_a_region_missing_its_leave_automation_is_caught(conn, config):
    """The headline case: arrivals land, so nothing else complains."""
    now = utc(2026, 3, 2, 10)
    for i in range(5):
        add_event(conn, "henry_hall", "enter", now - timedelta(days=i))
    add_event(conn, "denunzio", "enter", now)
    add_event(conn, "denunzio", "exit", now + timedelta(hours=1))
    add_event(conn, "prospect_club", "enter", now)
    add_event(conn, "prospect_club", "exit", now + timedelta(hours=1))

    rep = run_setup_check(conn, config)
    assert not rep.ok
    problem = next(p for p in rep.problems if p.startswith("Henry Hall"))
    assert "'Leave' automation is missing" in problem

    # ...and the ordinary health check is perfectly happy, which is the point.
    assert run_doctor(conn, config, now + timedelta(hours=1)).ok


def test_a_region_missing_its_arrive_automation_is_caught(conn, config):
    now = utc(2026, 3, 2, 10)
    add_event(conn, "henry_hall", "exit", now)
    rep = run_setup_check(conn, config)
    assert any("'Arrive' automation is missing" in p for p in rep.problems)


def test_an_unused_region_is_a_note_not_a_problem(conn, config):
    """firestone isn't in any commitment, so its silence isn't an incident."""
    now = utc(2026, 3, 2, 10)
    for region in ("henry_hall", "denunzio", "prospect_club", "nassau_starbucks"):
        add_event(conn, region, "enter", now)
        add_event(conn, region, "exit", now + timedelta(minutes=30))
    add_transit(conn, "henry_hall", "denunzio", 400, now - timedelta(days=1))
    add_transit(conn, "henry_hall", "prospect_club", 400, now - timedelta(days=2))
    config.regions["firestone"] = Region(
        id="firestone", lat=None, lon=None, radius_m=100
    )

    rep = run_setup_check(conn, config)
    assert rep.ok
    assert any("firestone" in n for n in rep.notes)
    assert not any("firestone" in p for p in rep.problems)


def test_a_leg_with_no_completed_trips_is_flagged(conn, config):
    """Both endpoints healthy, but the journey between them never recorded."""
    now = utc(2026, 3, 2, 10)
    for region in ("henry_hall", "denunzio", "prospect_club", "nassau_starbucks"):
        add_event(conn, region, "enter", now)
        add_event(conn, region, "exit", now + timedelta(minutes=30))

    rep = run_setup_check(conn, config)
    assert not rep.ok
    assert any("Swim practice" in p and "no completed trips" in p for p in rep.problems)


def test_a_thin_leg_is_a_note_not_a_problem(conn, config):
    """Some data, but not yet enough to stop using the fallback."""
    now = utc(2026, 3, 2, 10)
    for region in ("henry_hall", "denunzio", "prospect_club", "nassau_starbucks"):
        add_event(conn, region, "enter", now)
        add_event(conn, region, "exit", now + timedelta(minutes=30))
    add_transit(conn, "henry_hall", "denunzio", 400, now - timedelta(days=1))
    add_transit(conn, "henry_hall", "denunzio", 420, now - timedelta(days=2))
    add_transit(conn, "henry_hall", "prospect_club", 400, now - timedelta(days=3))

    rep = run_setup_check(conn, config)
    assert any("2 of 5 trips needed" in n for n in rep.notes)


def test_a_fully_wired_setup_passes(conn, config):
    now = utc(2026, 3, 2, 10)
    for region in ("henry_hall", "denunzio", "prospect_club", "nassau_starbucks"):
        add_event(conn, region, "enter", now)
        add_event(conn, region, "exit", now + timedelta(minutes=30))
    for i in range(6):
        add_transit(conn, "henry_hall", "denunzio", 400, now - timedelta(days=i + 1))
        add_transit(
            conn, "henry_hall", "prospect_club", 380,
            now - timedelta(days=i + 1, hours=6),
        )

    rep = run_setup_check(conn, config)
    assert rep.ok
    assert rep.problems == []
    assert rep.notes == []

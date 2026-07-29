"""Departure maths, DST correctness, and the travel estimator."""

from datetime import date, timedelta

from geofence.predict import current_region, estimate_travel
from geofence.schedule import due_now, local_datetime, occurrences_for_date, stale

from .conftest import add_event, add_transit, make_config, utc


def test_arrive_by_is_local_wall_clock_across_dst():
    # 6:00am local is 10:00 UTC in EST (winter) and 11:00... no: EST is UTC-5
    # so 6am -> 11:00 UTC; EDT is UTC-4 so 6am -> 10:00 UTC. The wall clock
    # stays 6am on both sides of the change -- that's the whole point.
    winter = local_datetime(date(2026, 1, 15), "06:00", "America/New_York")
    summer = local_datetime(date(2026, 7, 15), "06:00", "America/New_York")
    assert winter.hour == 11
    assert summer.hour == 10


def test_dst_change_morning_still_pings_at_local_six(conn):
    # US DST 2026 begins Sunday 8 March. Monday the 9th is the first practice
    # after the change: it must still be 6:00am local, not 5 or 7.
    config = make_config()
    occs = occurrences_for_date(conn, config, date(2026, 3, 9))
    swim = next(o for o in occs if o.id == "swim_practice")
    local = swim.arrive_by_utc.astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York")
    )
    assert (local.hour, local.minute) == (6, 0)


def test_departure_maths(conn):
    config = make_config()
    occs = occurrences_for_date(conn, config, date(2026, 3, 2))  # a Monday
    swim = next(o for o in occs if o.id == "swim_practice")
    # No data yet -> fallback 600s travel, + 300s margin => leave 15 min early,
    # ping a further prep_sec (900s) before that.
    assert (swim.arrive_by_utc - swim.must_leave_utc).total_seconds() == 900
    assert (swim.must_leave_utc - swim.ping_at_utc).total_seconds() == 900


def test_no_commitments_on_a_weekend(conn):
    config = make_config()
    assert occurrences_for_date(conn, config, date(2026, 3, 7)) == []  # Saturday


def test_estimate_falls_back_below_min_samples(conn):
    config = make_config()
    now = utc(2026, 3, 2, 10)
    for i in range(4):  # min_samples is 5
        add_transit(conn, "henry_hall", "denunzio", 400, now - timedelta(days=i + 1))
    est = estimate_travel(conn, config, "henry_hall", "denunzio", 600, now)
    assert est.source == "fallback"
    assert est.seconds == 600
    assert "not enough data" in est.describe()


def test_estimate_uses_p80_once_there_is_data(conn):
    config = make_config()
    now = utc(2026, 3, 2, 10)
    # Ten trips: mostly 400s, a couple of slow ones. p80 must sit above the
    # median -- being on time is the goal, not describing the typical day.
    for i, dur in enumerate([380, 390, 400, 400, 410, 420, 430, 440, 600, 700]):
        add_transit(conn, "henry_hall", "denunzio", dur, now - timedelta(days=i + 1))
    est = estimate_travel(conn, config, "henry_hall", "denunzio", 600, now)
    assert est.source == "observed"
    assert est.n == 10
    assert 440 <= est.seconds <= 620
    assert "10 trips" in est.describe()


def test_estimate_ignores_incomplete_and_suspect_segments(conn):
    """One bad day (dead phone -> a 'four-hour walk') can't move a departure."""
    config = make_config()
    now = utc(2026, 3, 2, 10)
    for i in range(6):
        add_transit(conn, "henry_hall", "denunzio", 400, now - timedelta(days=i + 1))
    add_transit(
        conn, "henry_hall", "denunzio", 14400, now - timedelta(days=7),
        status="incomplete",
    )
    add_transit(
        conn, "henry_hall", "denunzio", 5, now - timedelta(days=8), status="suspect"
    )
    est = estimate_travel(conn, config, "henry_hall", "denunzio", 600, now)
    assert est.n == 6
    assert est.seconds == 400


def test_estimate_ignores_data_outside_the_lookback(conn):
    config = make_config()
    now = utc(2026, 3, 2, 10)
    for i in range(8):  # 90 days ago, outside the 60-day window
        add_transit(conn, "henry_hall", "denunzio", 400, now - timedelta(days=90 + i))
    est = estimate_travel(conn, config, "henry_hall", "denunzio", 600, now)
    assert est.source == "fallback"


def test_due_now_fires_inside_the_window_only(conn):
    config = make_config()
    day = date(2026, 3, 2)
    swim = next(
        o for o in occurrences_for_date(conn, config, day) if o.id == "swim_practice"
    )
    assert due_now(conn, config, swim.ping_at_utc - timedelta(seconds=30)) == []
    assert [o.id for o in due_now(conn, config, swim.ping_at_utc)] == ["swim_practice"]
    # Past max_late_send_sec (300s) it is no longer sendable -- it's stale.
    late = swim.ping_at_utc + timedelta(seconds=400)
    assert due_now(conn, config, late) == []
    assert "swim_practice" in [o.id for o in stale(conn, config, late)]


def test_current_region_tracks_last_unclosed_enter(conn):
    now = utc(2026, 3, 2, 10)
    assert current_region(conn) is None
    add_event(conn, "henry_hall", "enter", now)
    assert current_region(conn) == "henry_hall"
    add_event(conn, "henry_hall", "exit", now + timedelta(minutes=5))
    assert current_region(conn) is None  # in transit
    add_event(conn, "denunzio", "enter", now + timedelta(minutes=15))
    assert current_region(conn) == "denunzio"


def test_current_region_ignores_suppressed_flaps(conn):
    now = utc(2026, 3, 2, 10)
    add_event(conn, "henry_hall", "enter", now)
    add_event(conn, "henry_hall", "exit", now + timedelta(seconds=20), suppressed=1)
    assert current_region(conn) == "henry_hall"

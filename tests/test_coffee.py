"""The coffee planner: does it respect the schedule and use measured times?"""

from datetime import timedelta

from geofence.coffee import best_window, find_windows, format_suggestion
from geofence.schedule import occurrences_for_date, today_local

from .conftest import add_dwell, add_event, add_transit, make_config, utc


def test_free_morning_says_go_now(conn, config):
    now = utc(2026, 3, 2, 15)     # 10:00 local, well before the 12:15 lunch
    add_event(conn, "henry_hall", "enter", now - timedelta(hours=1))
    w = best_window(conn, config, now)
    assert w is not None
    assert w.from_region == "henry_hall"
    assert "Best time" in format_suggestion(conn, config, now)


def test_window_too_tight_before_lunch_is_rejected(conn, config):
    """Round trip (10+10) + 10 in the shop + 5 slack won't fit in 12 minutes."""
    now = utc(2026, 3, 2, 16, 48)  # 11:48 local; lunch must-leave is 12:00
    add_event(conn, "henry_hall", "enter", now - timedelta(hours=2))
    first = find_windows(conn, config, now)[0]
    assert not first.feasible
    assert "too tight" in first.reason


def test_it_offers_the_gap_after_a_commitment(conn, config):
    """Blocked right now, but free once practice is over."""
    now = utc(2026, 3, 2, 10, 40)   # 05:40 local — practice leaves at 05:45
    add_event(conn, "henry_hall", "enter", now - timedelta(hours=8))
    windows = find_windows(conn, config, now)
    assert not windows[0].feasible              # can't go before practice
    after_practice = windows[1]
    assert after_practice.feasible
    assert after_practice.from_region == "denunzio"
    # Bounded by lunch, so it must name the constraint.
    assert after_practice.bounded_by.id == "club_lunch"


def test_measured_dwell_sets_when_practice_is_over(conn, config):
    """The window after practice starts from YOUR median stay, not a guess."""
    now = utc(2026, 3, 2, 10, 40)
    config.coffee.earliest = "05:00"   # don't let the hours clamp mask the maths
    add_event(conn, "henry_hall", "enter", now - timedelta(hours=8))
    # Six recorded 45-minute stays at the pool.
    for i in range(6):
        add_dwell(conn, "denunzio", 2700, now - timedelta(days=i + 1))

    after_practice = find_windows(conn, config, now)[1]
    arrive_by = next(
        o for o in occurrences_for_date(conn, config, today_local(config, now))
        if o.id == "swim_practice"
    ).arrive_by_utc
    assert after_practice.start_utc == arrive_by + timedelta(seconds=2700)


def test_planner_uses_measured_travel_times(conn, config):
    now = utc(2026, 3, 2, 15)
    add_event(conn, "henry_hall", "enter", now - timedelta(hours=1))
    for i in range(6):   # a brisk 4-minute walk to Nassau St, measured
        add_transit(
            conn, "henry_hall", "nassau_starbucks", 240, now - timedelta(days=i + 1)
        )
    w = best_window(conn, config, now)
    assert w.out_sec == 240      # not the 600s fallback


def test_no_room_at_all_explains_why(conn, config):
    """Late at night, past the coffee window: say so rather than shrug."""
    now = utc(2026, 3, 3, 4)      # 23:00 local
    add_event(conn, "henry_hall", "enter", now - timedelta(hours=2))
    assert best_window(conn, config, now) is None
    text = format_suggestion(conn, config, now)
    assert "No room for coffee" in text

"""The silent-data alarm and the weekly digest."""

from datetime import timedelta

from geofence import alerts, digest, health
from geofence.grading import LATE, ON_TIME, SKIPPED
from geofence.ingest.timeutil import iso_utc

from .conftest import add_event, add_transit, utc


# --- health watch ------------------------------------------------------------


def test_silence_warns_once_then_holds_its_tongue(conn, config, client, fake_tg):
    """The warning must be loud, then quiet — not a drumbeat you learn to swipe."""
    last = utc(2026, 3, 2, 0)
    add_event(conn, "henry_hall", "enter", last)

    silent = last + timedelta(hours=8)   # threshold is 6h
    assert health.check(conn, config, client, silent).kind == "warned"
    assert "No geofence data" in fake_tg.texts[0]
    assert "Run Immediately" in fake_tg.texts[0]     # the actual iOS fix
    assert "Henry Hall" in fake_tg.texts[0]

    # Still silent an hour later: no second warning.
    assert health.check(conn, config, client, silent + timedelta(hours=1)).kind is None
    assert len(fake_tg.sent) == 1


def test_silence_is_repeated_after_the_repeat_window(conn, config, client, fake_tg):
    last = utc(2026, 3, 2, 0)
    add_event(conn, "henry_hall", "enter", last)
    health.check(conn, config, client, last + timedelta(hours=8))
    # silence_repeat_hours defaults to 12.
    assert health.check(conn, config, client, last + timedelta(hours=21)).kind == "warned"
    assert len(fake_tg.sent) == 2


def test_recovery_is_announced(conn, config, client, fake_tg):
    """Saying "it's fixed" is what makes the warning trustworthy."""
    last = utc(2026, 3, 2, 0)
    add_event(conn, "henry_hall", "enter", last)
    silent = last + timedelta(hours=8)
    health.check(conn, config, client, silent)

    add_event(conn, "denunzio", "enter", silent)
    action = health.check(conn, config, client, silent + timedelta(minutes=5))
    assert action.kind == "recovered"
    assert "flowing again" in fake_tg.texts[-1]

    # And it doesn't keep congratulating you.
    assert health.check(conn, config, client, silent + timedelta(hours=1)).kind is None


def test_healthy_stream_says_nothing(conn, config, client, fake_tg):
    now = utc(2026, 3, 2, 10)
    add_event(conn, "henry_hall", "enter", now - timedelta(hours=1))
    assert health.check(conn, config, client, now).kind is None
    assert fake_tg.sent == []


def test_fresh_install_is_not_an_incident(conn, config, client, fake_tg):
    """No events EVER means "not set up yet", not "something broke"."""
    assert health.check(conn, config, client, utc(2026, 3, 2, 10)).kind is None
    assert fake_tg.sent == []


def test_a_backfilled_event_does_not_fire_a_false_alarm(conn, config, client, fake_tg):
    """Freshness is MAX(received), not the last row inserted — a replayed or
    out-of-order payload must not make current data look ancient."""
    now = utc(2026, 3, 2, 10)
    add_event(conn, "henry_hall", "enter", now - timedelta(minutes=30))
    add_event(conn, "denunzio", "enter", now - timedelta(days=20))   # arrives late
    assert health.check(conn, config, client, now).kind is None
    assert fake_tg.sent == []


def test_a_warning_that_could_not_be_delivered_is_retried(conn, config, client, fake_tg):
    last = utc(2026, 3, 2, 0)
    add_event(conn, "henry_hall", "enter", last)
    silent = last + timedelta(hours=8)

    fake_tg.fail_sends = True
    assert health.check(conn, config, client, silent).kind is None
    assert alerts.state_get(conn, "health_state") != "silent"   # not marked done

    fake_tg.fail_sends = False
    assert health.check(conn, config, client, silent).kind == "warned"


# --- weekly digest -----------------------------------------------------------


def _graded(conn, commitment_id, outcome, arrive_by):
    conn.execute(
        "INSERT INTO alerts (commitment_id, occurrence_date, kind, planned_depart_utc, "
        " arrive_by_utc, travel_estimate_sec, estimate_source, estimate_n, sent_at_utc, "
        " outcome) VALUES (?, ?, 'departure', ?, ?, 600, 'observed', 20, ?, ?)",
        (
            commitment_id,
            arrive_by.date().isoformat(),
            iso_utc(arrive_by - timedelta(minutes=15)),
            iso_utc(arrive_by),
            iso_utc(arrive_by - timedelta(minutes=30)),
            outcome,
        ),
    )
    conn.commit()


def test_digest_reports_the_on_time_record_and_real_walk_time(conn, config):
    now = utc(2026, 3, 8, 23)
    for i in range(4):
        _graded(conn, "swim_practice", ON_TIME, now - timedelta(days=i + 1))
    _graded(conn, "swim_practice", LATE, now - timedelta(days=5))
    _graded(conn, "swim_practice", SKIPPED, now - timedelta(days=6))
    for i in range(8):
        add_transit(conn, "henry_hall", "denunzio", 420, now - timedelta(days=i + 1))

    text = digest.build(conn, config, now)
    assert "On time 4/5" in text
    assert "skipped 1" in text
    assert "Walk: typically 7 min" in text


def test_digest_flags_a_week_of_broken_data(conn, config):
    """Numbers you shouldn't act on should say so."""
    now = utc(2026, 3, 8, 23)
    add_transit(conn, "henry_hall", "denunzio", 400, now - timedelta(days=1))
    for i in range(4):
        add_transit(
            conn, "henry_hall", "denunzio", 400, now - timedelta(days=i + 2),
            status="incomplete",
        )
    assert "incomplete or suspect" in digest.build(conn, config, now)


def _running_since(conn, period: str):
    """Simulate an agent that has already sent the digest for `period`."""
    alerts.state_set(conn, "digest_last_period", period)


def test_installing_does_not_fire_a_digest_immediately(conn, config, client, fake_tg):
    """Nobody wants a "your week" report 30 seconds after setup."""
    assert digest.maybe_send(conn, config, client, utc(2026, 3, 4, 15)) is False
    assert fake_tg.sent == []
    # It has adopted the current period, so the NEXT Sunday is the first send.
    assert digest.maybe_send(conn, config, client, utc(2026, 3, 9, 0)) is True


def test_digest_goes_out_once_a_week(conn, config, client, fake_tg):
    _running_since(conn, "2026-03-01")
    sunday_evening = utc(2026, 3, 9, 0)      # Sunday 8 March, 19:00 local
    assert digest.maybe_send(conn, config, client, sunday_evening) is True
    assert digest.maybe_send(conn, config, client, sunday_evening + timedelta(hours=2)) is False
    assert len(fake_tg.sent) == 1


def test_digest_is_not_sent_early(conn, config, client, fake_tg):
    _running_since(conn, "2026-03-01")
    sunday_morning = utc(2026, 3, 8, 14)     # Sunday 10:00 local, before 19:00
    assert digest.maybe_send(conn, config, client, sunday_morning) is False
    saturday = utc(2026, 3, 8, 0)            # Saturday 19:00 local
    assert digest.maybe_send(conn, config, client, saturday) is False


def test_a_late_boot_still_gets_that_weeks_digest(conn, config, client, fake_tg):
    """Box off all Sunday evening, back Monday: late beats never."""
    _running_since(conn, "2026-03-01")
    monday = utc(2026, 3, 9, 14)             # Monday 10:00 local
    assert digest.maybe_send(conn, config, client, monday) is True
    assert len(fake_tg.sent) == 1
    # And it doesn't then send again on its own Sunday.
    assert digest.maybe_send(conn, config, client, utc(2026, 3, 9, 23)) is False


def test_a_long_outage_sends_one_digest_not_a_backlog(conn, config, client, fake_tg):
    _running_since(conn, "2026-02-01")
    assert digest.maybe_send(conn, config, client, utc(2026, 3, 9, 0)) is True
    assert digest.maybe_send(conn, config, client, utc(2026, 3, 9, 1)) is False
    assert len(fake_tg.sent) == 1


def test_digest_can_be_switched_off(conn, config, client, fake_tg):
    _running_since(conn, "2026-03-01")
    config.digest.enabled = False
    assert digest.maybe_send(conn, config, client, utc(2026, 3, 9, 0)) is False


def test_undelivered_digest_is_retried_not_marked_sent(conn, config, client, fake_tg):
    _running_since(conn, "2026-03-01")
    sunday_evening = utc(2026, 3, 9, 0)
    fake_tg.fail_sends = True
    assert digest.maybe_send(conn, config, client, sunday_evening) is False
    fake_tg.fail_sends = False
    assert digest.maybe_send(conn, config, client, sunday_evening) is True

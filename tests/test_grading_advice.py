"""Grading outcomes and the leave-earlier/later advice built on top of them."""

from datetime import date, timedelta

from geofence import alerts, grading
from geofence.advice import advise_commitment
from geofence.agent import send_due
from geofence.ingest.timeutil import iso_utc
from geofence.schedule import occurrences_for_date

from .conftest import add_event, make_config, utc


def swim_occ(conn, config, day=date(2026, 3, 2)):
    return next(
        o for o in occurrences_for_date(conn, config, day) if o.id == "swim_practice"
    )


def _sent_alert(conn, config, client, day=date(2026, 3, 2)):
    occ = swim_occ(conn, config, day)
    return occ, send_due(conn, config, client, occ.ping_at_utc)[0]


def test_arriving_before_the_deadline_grades_on_time(conn, client, config):
    occ, alert_id = _sent_alert(conn, config, client)
    add_event(conn, "henry_hall", "exit", occ.must_leave_utc)
    add_event(conn, "denunzio", "enter", occ.arrive_by_utc - timedelta(minutes=3))

    graded = grading.grade_pending(conn, config, occ.arrive_by_utc + timedelta(hours=1))
    assert [g.outcome for g in graded] == [grading.ON_TIME]
    assert graded[0].slack_sec == 180


def test_arriving_after_the_deadline_grades_late(conn, client, config):
    occ, alert_id = _sent_alert(conn, config, client)
    add_event(conn, "henry_hall", "exit", occ.must_leave_utc + timedelta(minutes=6))
    add_event(conn, "denunzio", "enter", occ.arrive_by_utc + timedelta(minutes=4))

    graded = grading.grade_pending(conn, config, occ.arrive_by_utc + timedelta(hours=1))
    assert graded[0].outcome == grading.LATE
    assert graded[0].slack_sec == -240
    assert graded[0].depart_delta_sec == 360   # you left 6 min after you should


def test_never_arriving_grades_no_show_not_late(conn, client, config):
    """A forgotten tap must not masquerade as chronic lateness."""
    occ, _ = _sent_alert(conn, config, client)
    graded = grading.grade_pending(conn, config, occ.arrive_by_utc + timedelta(hours=1))
    assert graded[0].outcome == grading.NO_SHOW


def test_grading_waits_until_events_have_settled(conn, client, config):
    occ, _ = _sent_alert(conn, config, client)
    # One second after the deadline, a punctual arrival may not have landed yet.
    assert grading.grade_pending(conn, config, occ.arrive_by_utc + timedelta(seconds=1)) == []


def _seed_graded(conn, config, outcomes, base_day=date(2026, 2, 2)):
    """Write graded alerts directly: (outcome, slack_sec, depart_delta_sec).

    Walks weekdays only — practice doesn't run at the weekend, so a naive
    day+1 would ask for an occurrence that doesn't exist.
    """
    day = base_day
    for outcome, slack, delta in outcomes:
        while day.weekday() > 4:
            day += timedelta(days=1)
        occ = swim_occ(conn, config, day)
        day += timedelta(days=1)
        conn.execute(
            "INSERT INTO alerts (commitment_id, occurrence_date, kind, "
            " planned_depart_utc, arrive_by_utc, travel_estimate_sec, estimate_source, "
            " estimate_n, sent_at_utc, response, outcome, actual_depart_utc, "
            " actual_arrive_utc, graded_at_utc) "
            "VALUES (?, ?, 'departure', ?, ?, 600, 'observed', 20, ?, 'on_my_way', ?, "
            " ?, ?, ?)",
            (
                "swim_practice",
                occ.occurrence_date,
                iso_utc(occ.must_leave_utc),
                iso_utc(occ.arrive_by_utc),
                iso_utc(occ.ping_at_utc),
                outcome,
                iso_utc(occ.must_leave_utc + timedelta(seconds=delta)),
                iso_utc(occ.arrive_by_utc - timedelta(seconds=slack)),
                iso_utc(occ.arrive_by_utc),
            ),
        )
    conn.commit()


def test_advice_stays_quiet_until_there_is_enough_data(conn, config):
    _seed_graded(conn, config, [(grading.ON_TIME, 300, 0)] * 3)
    adv = advise_commitment(conn, config, config.commitments[0])
    assert adv.suggestions == []
    assert "Still learning" in adv.lines[0]


def test_habitual_lateness_suggests_leaving_earlier(conn, config):
    # 4 of 10 late at p80 (expected ~20%) -> the margin is too thin.
    rows = [(grading.LATE, -240, 60)] * 4 + [(grading.ON_TIME, 120, 0)] * 6
    _seed_graded(conn, config, rows)
    adv = advise_commitment(conn, config, config.commitments[0])
    assert "Leave earlier" in adv.lines[0]
    key, current, suggested = adv.suggestions[0]
    assert key == "safety_margin_sec"
    assert suggested > current


def test_habitual_earliness_suggests_leaving_later(conn, config):
    # Never late, always ~20 min early: you're standing around at the pool.
    _seed_graded(conn, config, [(grading.ON_TIME, 1200, 0)] * 10)
    adv = advise_commitment(conn, config, config.commitments[0])
    assert "leave later" in adv.lines[0]
    key, current, suggested = adv.suggestions[0]
    assert key == "safety_margin_sec"
    assert suggested < current


def test_good_timing_suggests_nothing(conn, config):
    _seed_graded(conn, config, [(grading.ON_TIME, 400, 0)] * 10)
    adv = advise_commitment(conn, config, config.commitments[0])
    assert adv.suggestions == []
    assert "Timing looks right" in adv.lines[0]


def test_dawdling_after_the_ping_suggests_more_prep_time(conn, config):
    _seed_graded(conn, config, [(grading.ON_TIME, 300, 420)] * 10)
    adv = advise_commitment(conn, config, config.commitments[0])
    keys = [k for k, _, _ in adv.suggestions]
    assert "prep_sec" in keys


def test_skips_and_missed_windows_never_reach_the_advice(conn, config):
    """Skipping is a choice, and a downed server measures the server."""
    rows = (
        [(grading.SKIPPED, 0, 0)] * 8
        + [(grading.MISSED_WINDOW, 0, 0)] * 5
        + [(grading.NO_SHOW, 0, 0)] * 4
    )
    _seed_graded(conn, config, rows)
    adv = advise_commitment(conn, config, config.commitments[0])
    assert adv.n == 0
    assert "Still learning" in adv.lines[0]

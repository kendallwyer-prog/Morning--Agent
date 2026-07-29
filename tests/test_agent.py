"""The agent loop: idempotent sends, missed windows, responses, nudges.

These cover the failure modes that would actually make you stop trusting the
thing — being pinged twice, being pinged for a practice you've already missed,
or tapping a button and having it not stick.
"""

from datetime import date, timedelta

from geofence import alerts, grading
from geofence.agent import poll_updates, send_due, send_nudges, tick
from geofence.schedule import occurrences_for_date

from .conftest import add_event, make_config, utc


def swim_occ(conn, config, day=date(2026, 3, 2)):
    return next(
        o for o in occurrences_for_date(conn, config, day) if o.id == "swim_practice"
    )


def test_alert_is_sent_once_and_only_once(conn, client, fake_tg):
    """Two ticks in the same send window must not ping twice."""
    config = make_config()
    now = swim_occ(conn, config).ping_at_utc

    assert len(send_due(conn, config, client, now)) == 1
    assert len(send_due(conn, config, client, now + timedelta(seconds=20))) == 0
    assert len(fake_tg.sent) == 1
    assert "Swim practice" in fake_tg.texts[0]
    assert "05:45" in fake_tg.texts[0] or "5:45" in fake_tg.texts[0]


def test_alert_offers_both_answers(conn, client, fake_tg):
    config = make_config()
    send_due(conn, config, client, swim_occ(conn, config).ping_at_utc)
    keyboard = fake_tg.sent[0]["reply_markup"]["inline_keyboard"][0]
    labels = [b["text"] for b in keyboard]
    assert any("On my way" in x for x in labels)
    assert any("Skipping" in x for x in labels)


def test_failed_send_is_retried_not_lost(conn, client, fake_tg):
    """A dropped connection at 5:37am must not swallow the alert."""
    config = make_config()
    now = swim_occ(conn, config).ping_at_utc

    fake_tg.fail_sends = True
    assert send_due(conn, config, client, now) == []
    assert fake_tg.sent == []

    fake_tg.fail_sends = False
    assert len(send_due(conn, config, client, now + timedelta(seconds=20))) == 1


def test_agent_down_over_departure_records_missed_window_and_stays_quiet(
    conn, client, fake_tg
):
    """A 'leave now' arriving after you should have gone is worse than silence."""
    config = make_config()
    occ = swim_occ(conn, config)
    late = occ.ping_at_utc + timedelta(hours=1)

    result = tick(conn, config, client, now=late, poll=False)
    assert result["sent"] == 0
    assert result["missed"] == 1
    assert fake_tg.sent == []

    row = conn.execute(
        "SELECT * FROM alerts WHERE commitment_id = 'swim_practice'"
    ).fetchone()
    assert row["outcome"] == grading.MISSED_WINDOW
    assert row["sent_at_utc"] is None


def test_on_my_way_is_recorded_and_the_message_is_rewritten(conn, client, fake_tg):
    config = make_config()
    now = swim_occ(conn, config).ping_at_utc
    alert_id = send_due(conn, config, client, now)[0]

    fake_tg.push_callback(1, f"omw:{alert_id}")
    poll_updates(conn, config, client, now + timedelta(seconds=30))

    row = alerts.get(conn, alert_id)
    assert row["response"] == alerts.ON_MY_WAY
    assert row["outcome"] is None          # still open: grading happens later
    assert "on your way" in fake_tg.edits[0]["text"]
    assert fake_tg.edits[0]["reply_markup"]["inline_keyboard"] == []


def test_skipping_closes_the_occurrence_without_counting_as_late(conn, client, fake_tg):
    config = make_config()
    now = swim_occ(conn, config).ping_at_utc
    alert_id = send_due(conn, config, client, now)[0]

    fake_tg.push_callback(1, f"skip:{alert_id}")
    poll_updates(conn, config, client, now + timedelta(seconds=30))

    row = alerts.get(conn, alert_id)
    assert row["response"] == alerts.SKIPPING
    assert row["outcome"] == grading.SKIPPED
    # And it stays skipped even after the arrival deadline passes.
    tick(conn, config, client, now=now + timedelta(hours=3), poll=False)
    assert alerts.get(conn, alert_id)["outcome"] == grading.SKIPPED


def test_second_tap_does_not_overwrite_the_first_answer(conn, client, fake_tg):
    config = make_config()
    now = swim_occ(conn, config).ping_at_utc
    alert_id = send_due(conn, config, client, now)[0]

    fake_tg.push_callback(1, f"omw:{alert_id}")
    poll_updates(conn, config, client, now)
    fake_tg.push_callback(2, f"skip:{alert_id}")
    poll_updates(conn, config, client, now + timedelta(minutes=1))

    assert alerts.get(conn, alert_id)["response"] == alerts.ON_MY_WAY
    assert "Already logged." in fake_tg.answered


def test_nudge_fires_once_when_you_said_omw_but_never_left(conn, client, fake_tg):
    config = make_config()
    occ = swim_occ(conn, config)
    alert_id = send_due(conn, config, client, occ.ping_at_utc)[0]
    alerts.record_response(conn, alert_id, alerts.ON_MY_WAY, occ.ping_at_utc)

    # Still in the dorm, 6 min past the departure time (nudge_after_sec = 300).
    late = occ.must_leave_utc + timedelta(seconds=360)
    assert len(send_nudges(conn, config, client, late)) == 1
    assert "still at Henry Hall" in fake_tg.texts[-1]
    # Never twice.
    assert send_nudges(conn, config, client, late + timedelta(minutes=5)) == []


def test_no_nudge_once_the_geofence_sees_you_leave(conn, client, fake_tg):
    config = make_config()
    occ = swim_occ(conn, config)
    alert_id = send_due(conn, config, client, occ.ping_at_utc)[0]
    alerts.record_response(conn, alert_id, alerts.ON_MY_WAY, occ.ping_at_utc)
    add_event(conn, "henry_hall", "exit", occ.must_leave_utc)

    late = occ.must_leave_utc + timedelta(seconds=360)
    assert send_nudges(conn, config, client, late) == []


def test_no_nudge_after_skipping(conn, client, fake_tg):
    config = make_config()
    occ = swim_occ(conn, config)
    alert_id = send_due(conn, config, client, occ.ping_at_utc)[0]
    alerts.record_response(conn, alert_id, alerts.SKIPPING, occ.ping_at_utc)
    assert send_nudges(conn, config, client, occ.must_leave_utc + timedelta(hours=1)) == []


def test_updates_from_another_chat_are_ignored(conn, client, fake_tg):
    """The bot is reachable by anyone who finds it; only you may drive it."""
    config = make_config()
    now = swim_occ(conn, config).ping_at_utc
    alert_id = send_due(conn, config, client, now)[0]

    fake_tg.push_callback(1, f"skip:{alert_id}", chat_id="999")
    poll_updates(conn, config, client, now)

    assert alerts.get(conn, alert_id)["response"] is None


def test_update_offset_persists_so_taps_survive_a_restart(conn, client, fake_tg):
    config = make_config()
    now = swim_occ(conn, config).ping_at_utc
    fake_tg.push_text(7, "/today")
    poll_updates(conn, config, client, now)
    assert alerts.state_get(conn, "telegram_update_offset") == "8"


def test_typed_reply_is_attributed_to_the_open_alert(conn, client, fake_tg):
    """Buttons are awkward on a locked screen; "omw" must still log."""
    config = make_config()
    now = swim_occ(conn, config).ping_at_utc
    alert_id = send_due(conn, config, client, now)[0]

    fake_tg.push_text(1, "omw")
    poll_updates(conn, config, client, now + timedelta(seconds=45))
    assert alerts.get(conn, alert_id)["response"] == alerts.ON_MY_WAY


def test_commands_answer(conn, client, fake_tg):
    config = make_config()
    now = utc(2026, 3, 2, 15)
    for i, cmd in enumerate(["/today", "/next", "/coffee", "/stats", "/advice", "/xyz"]):
        fake_tg.push_text(i + 1, cmd)
        poll_updates(conn, config, client, now)
    assert len(fake_tg.sent) == 6
    assert "Today" in fake_tg.texts[0]
    assert "☕" in fake_tg.texts[2]
    assert "Commands" in fake_tg.texts[5]   # unknown -> help

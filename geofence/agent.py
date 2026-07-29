"""The agent: one loop that pings you, listens for your answer, and grades.

A single tick does five things, in this order on purpose:

1. **Grade** anything ripe, so today's advice reflects yesterday.
2. **Close missed windows** — occurrences whose ping time passed while the
   agent was down. Recorded, never sent late (see ``schedule.stale``).
3. **Send** whatever is due. The alert row is claimed *before* the network
   call, so this can never double-ping you.
4. **Nudge** once if you said "on my way" and the geofence says you're still
   in your room past your departure time.
5. **Poll** Telegram for your taps and commands.

Grading before sending matters: it means the departure time you're told at
5:37am already incorporates the walk you took yesterday.

Everything is driven by ``now`` passed in from the caller, so the whole tick is
testable without waiting for 5:37am to come round.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import advice as advice_mod
from . import alerts, coffee, digest, grading, health
from .config import Config
from .ingest.timeutil import parse_iso
from .notify.telegram import (
    CB_ON_MY_WAY,
    CB_SKIPPING,
    TelegramClient,
    Update,
    parse_callback_data,
)
from .predict import current_region, first_event_between
from .schedule import Occurrence, due_now, stale, today_local, upcoming
from .stats import compute_stats, format_stats

log = logging.getLogger(__name__)

_OFFSET_KEY = "telegram_update_offset"


# --- message text ------------------------------------------------------------


def _fmt_local(dt: datetime, tz: str) -> str:
    return dt.astimezone(ZoneInfo(tz)).strftime("%-I:%M %p")


def departure_text(occ: Occurrence, config: Config, now: datetime) -> str:
    tz = config.timezone
    mins = max(0, int((occ.must_leave_utc - now).total_seconds() // 60))
    lead = "now" if mins == 0 else f"in {mins} min"
    return (
        f"🔔 <b>{occ.commitment.label}</b> — leave {lead}\n"
        f"Out the door by <b>{_fmt_local(occ.must_leave_utc, tz)}</b> to make "
        f"{config.region_name(occ.commitment.destination)} by "
        f"{_fmt_local(occ.arrive_by_utc, tz)}.\n"
        f"Travel: {occ.estimate.describe()}."
    )


def _buttons(alert_id: int) -> list[tuple[str, str]]:
    return [
        ("🏃 On my way", f"{CB_ON_MY_WAY}:{alert_id}"),
        ("🙅 Skipping today", f"{CB_SKIPPING}:{alert_id}"),
    ]


def today_text(conn: sqlite3.Connection, config: Config, now: datetime) -> str:
    tz = config.timezone
    today = today_local(config, now).isoformat()
    occs = [o for o in upcoming(conn, config, now, days=1) if o.occurrence_date == today]
    if not occs:
        return "Nothing scheduled today."
    lines = [f"<b>Today</b> ({today})"]
    for o in occs:
        past = "✓ " if o.must_leave_utc < now else ""
        lines.append(
            f"{past}{o.commitment.label}: leave {_fmt_local(o.must_leave_utc, tz)} "
            f"→ {config.region_name(o.commitment.destination)} by "
            f"{_fmt_local(o.arrive_by_utc, tz)} ({o.estimate.describe()})"
        )
    return "\n".join(lines)


_HELP = (
    "<b>Commands</b>\n"
    "/today — today's departure times\n"
    "/next — the next thing you have to leave for\n"
    "/coffee — when you can fit in a coffee run\n"
    "/where — where I think you are\n"
    "/advice — should you be leaving earlier or later\n"
    "/digest — this week's summary (also sent weekly)\n"
    "/setup — is every geofence reporting both directions?\n"
    "/stats — measured travel times\n"
    "/help — this message\n\n"
    "You can also reply <code>omw</code> or <code>skip</code> instead of "
    "tapping the buttons."
)


# --- tick phases -------------------------------------------------------------


def send_due(
    conn: sqlite3.Connection, config: Config, client: TelegramClient, now: datetime
) -> list[int]:
    """Claim + send every alert whose ping time has arrived."""
    sent: list[int] = []
    for occ in due_now(conn, config, now):
        alert_id = alerts.claim(conn, occ)
        if alert_id is None:
            continue  # already claimed -> already pinged (or being pinged)
        result = client.send(departure_text(occ, config, now), _buttons(alert_id))
        if not result.ok:
            # Release the claim so the next tick retries while still inside the
            # send window. Past that window it becomes a missed_window instead.
            alerts.drop(conn, alert_id)
            log.warning("send failed for %s; will retry", occ.commitment.id)
            continue
        alerts.mark_sent(conn, alert_id, result.message_id, now)
        sent.append(alert_id)
    return sent


def close_missed(conn: sqlite3.Connection, config: Config, now: datetime) -> list[int]:
    """Record occurrences whose ping window passed unsent (agent was down)."""
    closed: list[int] = []
    for occ in stale(conn, config, now):
        alert_id = alerts.claim(conn, occ)
        if alert_id is None:
            continue
        alerts.set_outcome(conn, alert_id, grading.MISSED_WINDOW, now=now)
        log.warning(
            "missed alert window for %s on %s", occ.commitment.id, occ.occurrence_date
        )
        closed.append(alert_id)
    return closed


def send_nudges(
    conn: sqlite3.Connection, config: Config, client: TelegramClient, now: datetime
) -> list[int]:
    """One nudge, and only one, if "on my way" hasn't turned into leaving."""
    sent: list[int] = []
    grace = timedelta(seconds=config.alerting.nudge_after_sec)
    for row in alerts.ungraded(conn):
        if row["response"] != alerts.ON_MY_WAY:
            continue
        planned = parse_iso(row["planned_depart_utc"])
        if now < planned + grace:
            continue
        c = config.commitment(row["commitment_id"])
        if c is None:
            continue
        # Did the geofence see you actually go?
        left = first_event_between(
            conn, c.origin, "exit", parse_iso(row["sent_at_utc"]), now
        )
        if left is not None:
            continue

        cur = conn.execute(
            "INSERT OR IGNORE INTO alerts "
            "(commitment_id, occurrence_date, kind, planned_depart_utc, arrive_by_utc, "
            " travel_estimate_sec, estimate_source, estimate_n) "
            "VALUES (?, ?, 'nudge', ?, ?, ?, ?, ?)",
            (
                row["commitment_id"],
                row["occurrence_date"],
                row["planned_depart_utc"],
                row["arrive_by_utc"],
                row["travel_estimate_sec"],
                row["estimate_source"],
                row["estimate_n"],
            ),
        )
        conn.commit()
        if not cur.rowcount:
            continue  # already nudged for this occurrence
        nudge_id = cur.lastrowid
        late_min = int((now - planned).total_seconds() // 60)
        result = client.send(
            f"⏰ <b>{c.label}</b> — you said you were on your way "
            f"{late_min} min ago, but you're still at {config.region_name(c.origin)}."
        )
        if result.ok:
            alerts.mark_sent(conn, nudge_id, result.message_id, now)
            alerts.set_outcome(conn, nudge_id, "nudged", now=now)
            sent.append(nudge_id)
        else:
            alerts.drop(conn, nudge_id)
    return sent


# --- inbound handling --------------------------------------------------------


def handle_callback(
    conn: sqlite3.Connection, config: Config, client: TelegramClient, u: Update, now: datetime
) -> None:
    action, alert_id = parse_callback_data(u.data)
    if action is None or alert_id is None:
        return
    response = {CB_ON_MY_WAY: alerts.ON_MY_WAY, CB_SKIPPING: alerts.SKIPPING}.get(action)
    if response is None:
        return

    row = alerts.get(conn, alert_id)
    if row is None:
        client.answer_callback(u.callback_query_id or "", "That alert is gone.")
        return

    first = alerts.record_response(conn, alert_id, response, now)
    if not first:
        # Tapping again must not overwrite the first answer.
        client.answer_callback(u.callback_query_id or "", "Already logged.")
        return

    c = config.commitment(row["commitment_id"])
    label = c.label if c else row["commitment_id"]
    stamp = _fmt_local(now, config.timezone)
    if response == alerts.ON_MY_WAY:
        client.answer_callback(u.callback_query_id or "", "Nice. Go.")
        confirm = f"🏃 <b>{label}</b> — on your way, logged {stamp}."
    else:
        client.answer_callback(u.callback_query_id or "", "Logged as skipped.")
        confirm = (
            f"🙅 <b>{label}</b> — skipping today, logged {stamp}.\n"
            f"Won't count against your timings."
        )
    if row["telegram_message_id"]:
        client.edit_message(row["telegram_message_id"], confirm)
    else:
        client.send(confirm)


def handle_text(
    conn: sqlite3.Connection, config: Config, client: TelegramClient, u: Update, now: datetime
) -> None:
    text = (u.text or "").strip().lower().lstrip("/")
    cmd = text.split()[0] if text else ""

    if cmd in ("coffee", "☕"):
        client.send(coffee.format_suggestion(conn, config, now))
    elif cmd == "today":
        client.send(today_text(conn, config, now))
    elif cmd == "next":
        nxt = [o for o in upcoming(conn, config, now, days=2) if o.must_leave_utc > now]
        if not nxt:
            client.send("Nothing coming up.")
        else:
            o = nxt[0]
            client.send(departure_text(o, config, now))
    elif cmd == "where":
        here = current_region(conn)
        client.send(
            f"📍 You're at <b>{config.region_name(here)}</b>."
            if here
            else "📍 Not inside any geofence right now (in transit, or "
            "somewhere I don't track)."
        )
    elif cmd in ("digest", "week"):
        client.send(digest.build(conn, config, now))
    elif cmd == "setup":
        from .setup_check import format_setup, run_setup_check

        client.send("<pre>" + format_setup(run_setup_check(conn, config)) + "</pre>")
    elif cmd == "advice":
        client.send(
            "<pre>" + advice_mod.format_advice(advice_mod.advise_all(conn, config, now))
            + "</pre>"
        )
    elif cmd == "stats":
        client.send("<pre>" + format_stats(compute_stats(conn)) + "</pre>")
    elif cmd in ("omw", "on my way"):
        _reply_without_button(conn, config, client, now, alerts.ON_MY_WAY)
    elif cmd in ("skip", "skipping"):
        _reply_without_button(conn, config, client, now, alerts.SKIPPING)
    else:
        client.send(_HELP)


def _reply_without_button(
    conn: sqlite3.Connection,
    config: Config,
    client: TelegramClient,
    now: datetime,
    response: str,
) -> None:
    """Attribute a typed "omw"/"skip" to today's open alert — the buttons can
    be awkward to hit on a locked screen, and a reply you can't log is a reply
    that silently drops data."""
    today = today_local(config, now).isoformat()
    row = alerts.open_alert_for_today(conn, today)
    if row is None:
        client.send("Nothing open to answer right now.")
        return
    alerts.record_response(conn, row["id"], response, now)
    c = config.commitment(row["commitment_id"])
    label = c.label if c else row["commitment_id"]
    word = "on your way" if response == alerts.ON_MY_WAY else "skipping today"
    client.send(f"Logged: {label} — {word}.")


def poll_updates(
    conn: sqlite3.Connection, config: Config, client: TelegramClient, now: datetime
) -> int:
    """Fetch and dispatch inbound updates. Returns how many were handled."""
    raw_offset = alerts.state_get(conn, _OFFSET_KEY)
    offset = int(raw_offset) if raw_offset else None
    updates = client.get_updates(offset, config.telegram.poll_timeout_sec)

    handled = 0
    for u in updates:
        # Advance the cursor FIRST. A message that crashes a handler must not
        # be replayed forever, blocking every later update behind it.
        alerts.state_set(conn, _OFFSET_KEY, str(u.update_id + 1))
        # Ignore anyone who isn't you: the bot is reachable by anyone who finds
        # its username, and only the configured chat may drive it.
        if u.chat_id and str(u.chat_id) != str(config.telegram.chat_id):
            log.warning("ignoring update from unknown chat %s", u.chat_id)
            continue
        try:
            if u.kind == "callback":
                handle_callback(conn, config, client, u, now)
            else:
                handle_text(conn, config, client, u, now)
            handled += 1
        except Exception:
            log.exception("failed handling update %s", u.update_id)
    return handled


# --- the loop ----------------------------------------------------------------


def make_client(config: Config) -> TelegramClient:
    tg = config.telegram
    if not tg.bot_token or not tg.chat_id:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set. "
            "See README-geofence.md (Telegram setup)."
        )
    return TelegramClient(bot_token=tg.bot_token, chat_id=tg.chat_id)


def tick(
    conn: sqlite3.Connection,
    config: Config,
    client: TelegramClient,
    now: datetime | None = None,
    poll: bool = True,
) -> dict:
    now = now or datetime.now(timezone.utc)
    graded = grading.grade_pending(conn, config, now)
    missed = close_missed(conn, config, now)
    sent = send_due(conn, config, client, now)
    nudged = send_nudges(conn, config, client, now)
    # Housekeeping: the silent-data alarm and the weekly summary. Both are
    # self-throttling, so calling them on every tick is correct and cheap.
    watched = health.check(conn, config, client, now)
    digested = digest.maybe_send(conn, config, client, now)
    handled = poll_updates(conn, config, client, now) if poll else 0
    return {
        "graded": len(graded),
        "missed": len(missed),
        "sent": len(sent),
        "nudged": len(nudged),
        "health": watched.kind,
        "digest": digested,
        "updates": handled,
    }


def run_forever(conn: sqlite3.Connection, config: Config) -> None:
    client = make_client(config)
    log.info("agent started; %d commitments", len(config.commitments))
    while True:
        started = time.monotonic()
        try:
            tick(conn, config, client)
        except Exception:
            # The loop is the product. A bad tick must never stop tomorrow's
            # 5:37am ping, so every failure is logged and slept through.
            log.exception("tick failed")
        # Normally the 25s long-poll paces the loop for us. When Telegram is
        # unreachable getUpdates returns instantly, so sleep the remainder --
        # otherwise a network outage becomes a hot loop hammering the API.
        elapsed = time.monotonic() - started
        if elapsed < config.telegram.tick_sec:
            time.sleep(config.telegram.tick_sec - elapsed)

"""Command-line interface: `python -m geofence <command>`.

  serve                  run the FastAPI app (uvicorn)
  initdb                 create the SQLite schema
  reprocess              re-derive events + segments from raw_events
  stats                  median / p80 / n per segment type (suppresses n<10)
  doctor                 report whether events have stopped arriving
  calibrate add ...      record a ground-truth departure time
  calibrate estimate     suggest per-region calibration offsets
  agent                  run the Telegram alerting loop
  agent once             run a single tick (cron-friendly / for testing)
  plan                   show today's computed departure times
  coffee                 when to get coffee today
  advice                 should you be leaving earlier or later
  telegram test          send a test message to confirm the bot works
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from .advice import advise_all, format_advice
from .calibrate import add_ground_truth, estimate, format_estimates
from .config import load_config
from .db import connect, init_db
from .doctor import format_report, run_doctor
from .pipeline import reprocess
from .stats import compute_stats, format_stats

_DEFAULT_MIN_N = 10


def _conn(config):
    conn = connect(config.db_path)
    init_db(conn)
    return conn


def cmd_serve(args, config):
    import uvicorn

    # Imported lazily so `stats`/`doctor` don't require fastapi/uvicorn.
    uvicorn.run("geofence.api:app", host=args.host, port=args.port, reload=False)


def cmd_initdb(args, config):
    conn = _conn(config)
    print(f"Initialized schema at {config.db_path}")
    conn.close()


def cmd_reprocess(args, config):
    conn = _conn(config)
    info = reprocess(conn, config)
    conn.close()
    print(
        f"Reprocessed: {info['raw_total']} raw events -> "
        f"{info['events_rebuilt']} events ({info['raw_skipped']} skipped)."
    )


def cmd_stats(args, config):
    conn = _conn(config)
    buckets = compute_stats(conn, min_n=args.min_n)
    conn.close()
    print(format_stats(buckets, min_n=args.min_n))


def cmd_doctor(args, config):
    conn = _conn(config)
    rep = run_doctor(conn, config)
    conn.close()
    print(format_report(rep))
    sys.exit(0 if rep.ok else 1)


def cmd_calibrate_add(args, config):
    conn = _conn(config)
    region = add_ground_truth(conn, config, args.region, args.time, args.note)
    conn.close()
    print(f"Recorded ground-truth departure for {region} at {args.time}.")


def cmd_calibrate_estimate(args, config):
    conn = _conn(config)
    ests = estimate(conn, config)
    conn.close()
    print(format_estimates(ests))


def cmd_agent(args, config):
    from .agent import run_forever

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    conn = _conn(config)
    try:
        run_forever(conn, config)
    except KeyboardInterrupt:
        print("\nagent stopped.")
    finally:
        conn.close()


def cmd_agent_once(args, config):
    from .agent import make_client, tick

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    conn = _conn(config)
    result = tick(conn, config, make_client(config), poll=not args.no_poll)
    conn.close()
    print(
        f"tick: sent={result['sent']} nudged={result['nudged']} "
        f"graded={result['graded']} missed={result['missed']} "
        f"updates={result['updates']}"
    )


def cmd_plan(args, config):
    from .schedule import upcoming

    conn = _conn(config)
    now = datetime.now(timezone.utc)
    occs = upcoming(conn, config, now, days=args.days)
    conn.close()
    if not occs:
        print("No commitments scheduled. Add [[commitments]] to geofence.toml.")
        return
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(config.timezone)
    print(f"{'when':<18} {'commitment':<18} {'leave':<9} {'arrive':<9} travel")
    print("-" * 72)
    for o in occs:
        lv = o.must_leave_utc.astimezone(tz)
        ar = o.arrive_by_utc.astimezone(tz)
        print(
            f"{lv.strftime('%a %d %b'):<18} {o.commitment.label:<18} "
            f"{lv.strftime('%H:%M'):<9} {ar.strftime('%H:%M'):<9} "
            f"{o.estimate.describe()}"
        )


def cmd_coffee(args, config):
    from .coffee import format_suggestion

    conn = _conn(config)
    text = format_suggestion(conn, config, datetime.now(timezone.utc))
    conn.close()
    # The Telegram formatting is HTML; strip the two tags used for the console.
    print(text.replace("<b>", "").replace("</b>", ""))


def cmd_advice(args, config):
    conn = _conn(config)
    items = advise_all(conn, config, datetime.now(timezone.utc))
    conn.close()
    print(format_advice(items))


def cmd_telegram_test(args, config):
    from .agent import make_client

    client = make_client(config)
    result = client.send(
        "✅ Geofence agent connected. You'll get departure alerts here."
    )
    if result.ok:
        print("Sent. Check Telegram.")
    else:
        print(f"Failed: {result.error}", file=sys.stderr)
        sys.exit(1)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="geofence", description=__doc__)
    p.add_argument("--config", default=None, help="path to geofence.toml")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", help="run the FastAPI ingestion server")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)

    sub.add_parser("initdb", help="create the SQLite schema").set_defaults(func=cmd_initdb)

    sub.add_parser(
        "reprocess", help="re-derive events + segments from raw_events"
    ).set_defaults(func=cmd_reprocess)

    st = sub.add_parser("stats", help="median / p80 / n per segment type")
    st.add_argument("--min-n", type=int, default=_DEFAULT_MIN_N)
    st.set_defaults(func=cmd_stats)

    sub.add_parser("doctor", help="have events stopped arriving?").set_defaults(
        func=cmd_doctor
    )

    cal = sub.add_parser("calibrate", help="ground-truth departure calibration")
    cal_sub = cal.add_subparsers(dest="cal_command", required=True)
    ca = cal_sub.add_parser("add", help="record an actual departure time")
    ca.add_argument("region", help="region id or alias, e.g. henry_hall")
    ca.add_argument("time", help="ISO-8601 timestamp or epoch of actual departure")
    ca.add_argument("--note", default=None)
    ca.set_defaults(func=cmd_calibrate_add)
    cal_sub.add_parser(
        "estimate", help="suggest per-region offsets from ground truth"
    ).set_defaults(func=cmd_calibrate_estimate)

    ag = sub.add_parser("agent", help="run the Telegram alerting loop")
    ag.set_defaults(func=cmd_agent)
    ag_sub = ag.add_subparsers(dest="agent_command")
    once = ag_sub.add_parser("once", help="run a single tick and exit")
    once.add_argument(
        "--no-poll",
        action="store_true",
        help="skip getUpdates (don't consume pending taps)",
    )
    once.set_defaults(func=cmd_agent_once)

    pl = sub.add_parser("plan", help="today's computed departure times")
    pl.add_argument("--days", type=int, default=2)
    pl.set_defaults(func=cmd_plan)

    sub.add_parser("coffee", help="when to get coffee today").set_defaults(
        func=cmd_coffee
    )
    sub.add_parser("advice", help="leave earlier or later?").set_defaults(
        func=cmd_advice
    )

    tg = sub.add_parser("telegram", help="Telegram helpers")
    tg_sub = tg.add_subparsers(dest="telegram_command", required=True)
    tg_sub.add_parser("test", help="send a test message").set_defaults(
        func=cmd_telegram_test
    )

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        args.func(args, config)
    except (RuntimeError, ValueError) as e:
        # Config typos and missing credentials are the two things every user
        # hits first. A one-line message beats a traceback.
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

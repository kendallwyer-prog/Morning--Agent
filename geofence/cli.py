"""Command-line interface: `python -m geofence <command>`.

  serve                  run the FastAPI app (uvicorn)
  initdb                 create the SQLite schema
  reprocess              re-derive events + segments from raw_events
  stats                  median / p80 / n per segment type (suppresses n<10)
  doctor                 report whether events have stopped arriving
  calibrate add ...      record a ground-truth departure time
  calibrate estimate     suggest per-region calibration offsets
"""

from __future__ import annotations

import argparse
import sys

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

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    args.func(args, config)


if __name__ == "__main__":
    main()

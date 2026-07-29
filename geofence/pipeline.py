"""Ties raw ingestion -> normalized events -> derived segments together, and
provides the full re-derivation path (`reprocess`).

Data flow:
    POST body ── store_raw (immutable) ──▶ raw_events
                       │
                       └─ insert_event (normalize) ──▶ events
                                                          │
                                     rebuild ── debounce + build_segments ──▶ segments

`reprocess` throws away events + segments and rebuilds them from raw_events, so
changing config (calibration offsets, thresholds) or logic re-derives cleanly.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .config import Config
from .ingest.normalize import normalize
from .ingest.timeutil import iso_utc, parse_iso, to_local
from .models import NormalizedEvent, ProcEvent
from .processing.debounce import compute_suppressed
from .processing.segments import build_segments


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def store_raw(
    conn: sqlite3.Connection,
    dedupe_key: str,
    client_type: str,
    payload_text: str,
    source_ip: str | None,
    received_at_utc: str,
) -> tuple[int, bool]:
    """Insert an immutable raw event. Returns (raw_id, is_new). A duplicate
    dedupe_key is a no-op (idempotency) and returns the existing row id."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO raw_events "
        "(received_at_utc, client_type, dedupe_key, payload_text, source_ip) "
        "VALUES (?, ?, ?, ?, ?)",
        (received_at_utc, client_type, dedupe_key, payload_text, source_ip),
    )
    if cur.rowcount == 0:
        row = conn.execute(
            "SELECT id FROM raw_events WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        return row["id"], False
    return cur.lastrowid, True


def _calibrated(config: Config, region: str, transition: str, event_utc: datetime) -> datetime:
    """iOS fires 'leave' late; subtract the region offset from exits only."""
    if transition == "exit":
        return event_utc - timedelta(seconds=config.region_offset(region))
    return event_utc


def insert_event(conn: sqlite3.Connection, config: Config, raw_id: int, ne: NormalizedEvent) -> int:
    local, offset_min = to_local(ne.event_utc, config.timezone)
    calibrated = _calibrated(config, ne.region, ne.transition, ne.event_utc)
    cur = conn.execute(
        "INSERT INTO events "
        "(raw_id, region, transition, event_utc, event_local, tz_offset_min, "
        " lat, lon, acc, calibrated_utc, suppressed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (
            raw_id,
            ne.region,
            ne.transition,
            iso_utc(ne.event_utc),
            local.isoformat(),
            offset_min,
            ne.lat,
            ne.lon,
            ne.acc,
            iso_utc(calibrated),
        ),
    )
    return cur.lastrowid


def rebuild(conn: sqlite3.Connection, config: Config) -> None:
    """Recompute debounce + calibration for all events, then rebuild segments.
    Cheap for a personal-scale dataset, so it's run after every new event."""
    rows = conn.execute(
        "SELECT id, region, transition, event_utc FROM events ORDER BY event_utc, id"
    ).fetchall()

    # 1. Debounce on RAW recorded times.
    raw_procs = [
        ProcEvent(r["id"], r["region"], r["transition"], parse_iso(r["event_utc"]))
        for r in rows
    ]
    suppressed = set(compute_suppressed(raw_procs, config.flap_window_sec))

    # 2. Refresh calibration + suppressed flags; collect CALIBRATED survivors.
    conn.execute("UPDATE events SET suppressed = 0")
    procs: list[ProcEvent] = []
    for r in rows:
        ev_utc = parse_iso(r["event_utc"])
        cal = _calibrated(config, r["region"], r["transition"], ev_utc)
        conn.execute(
            "UPDATE events SET calibrated_utc = ? WHERE id = ?", (iso_utc(cal), r["id"])
        )
        if r["id"] in suppressed:
            conn.execute("UPDATE events SET suppressed = 1 WHERE id = ?", (r["id"],))
            continue
        procs.append(ProcEvent(r["id"], r["region"], r["transition"], cal))

    # 3. Rebuild segments.
    segs = build_segments(procs, config.max_transit_sec, config.min_transit)
    conn.execute("DELETE FROM segments")
    for s in segs:
        conn.execute(
            "INSERT INTO segments "
            "(segment_type, from_region, to_region, start_event_id, end_event_id, "
            " start_utc, end_utc, duration_sec, status, status_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                s.segment_type,
                s.from_region,
                s.to_region,
                s.start_event_id,
                s.end_event_id,
                iso_utc(s.start_utc),
                iso_utc(s.end_utc) if s.end_utc else None,
                s.duration_sec,
                s.status,
                s.status_reason,
            ),
        )


def ingest(
    conn: sqlite3.Connection,
    config: Config,
    payload: dict,
    payload_text: str,
    source_ip: str | None,
) -> dict:
    """Normalize + store one already-parsed payload, then rebuild. Assumes the
    caller has verified auth and JSON-parsed the body."""
    ne = normalize(payload, config)  # may raise ValueError
    raw_id, is_new = store_raw(
        conn, ne.dedupe_key, ne.client_type, payload_text, source_ip, now_utc_iso()
    )
    if is_new:
        insert_event(conn, config, raw_id, ne)
        rebuild(conn, config)
    conn.commit()
    return {
        "status": "ok",
        "duplicate": not is_new,
        "region": ne.region,
        "transition": ne.transition,
    }


def reprocess(conn: sqlite3.Connection, config: Config) -> dict:
    """Re-derive EVERYTHING from raw_events. Wipes events + segments, then
    re-normalizes every raw payload and rebuilds. Raw rows that no longer
    normalize (e.g. a region you removed) are skipped but retained."""
    conn.execute("DELETE FROM segments")
    conn.execute("DELETE FROM events")
    rows = conn.execute("SELECT id, payload_text FROM raw_events ORDER BY id").fetchall()
    import json

    kept = 0
    skipped = 0
    for r in rows:
        try:
            ne = normalize(json.loads(r["payload_text"]), config)
        except Exception:
            skipped += 1
            continue
        insert_event(conn, config, r["id"], ne)
        kept += 1
    rebuild(conn, config)
    conn.commit()
    return {"raw_total": len(rows), "events_rebuilt": kept, "raw_skipped": skipped}

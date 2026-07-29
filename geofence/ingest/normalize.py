"""Normalize the two supported client payloads into one ``NormalizedEvent``.

Client type is auto-detected from the payload shape, so a single endpoint
serves both:

  * OwnTracks  -> ``{"_type": "transition", "event": "enter"|"leave", ...}``
  * Shortcuts  -> whatever JSON you configure the Shortcut to POST
                  (see README-geofence.md for the exact shape).
"""

from __future__ import annotations

import hashlib

from ..config import Config
from ..models import NormalizedEvent
from .timeutil import parse_utc

_ENTER = {"enter", "arrive", "arrived", "entered", "entry", "in"}
_EXIT = {"exit", "leave", "left", "leaving", "exited", "departed", "depart", "out"}


def _canon_transition(v) -> str:
    s = str(v).strip().lower()
    if s in _ENTER:
        return "enter"
    if s in _EXIT:
        return "exit"
    raise ValueError(f"unknown transition: {v!r}")


def _f(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def detect_client(payload: dict) -> str:
    if isinstance(payload, dict) and payload.get("_type") == "transition":
        return "owntracks"
    return "shortcuts"


def normalize(payload: dict, config: Config) -> NormalizedEvent:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    if detect_client(payload) == "owntracks":
        return _normalize_owntracks(payload, config)
    return _normalize_shortcuts(payload, config)


def _normalize_owntracks(p: dict, config: Config) -> NormalizedEvent:
    # OwnTracks region name lives in `desc` (falls back to `t`, a short tag).
    raw_region = p.get("desc") or p.get("t") or ""
    region = config.canonical_region(raw_region)
    if region is None:
        raise ValueError(f"unknown region: {raw_region!r}")

    transition = _canon_transition(p.get("event", ""))

    tst = p.get("tst")
    if tst is None:
        raise ValueError("owntracks payload missing 'tst'")
    event_utc = parse_utc(tst)

    # OwnTracks transitions carry no stable client UUID, so we synthesize a
    # deterministic dedupe key. Same true double-fire => same key => idempotent.
    tid = str(p.get("tid", ""))
    key_src = f"owntracks|{tid}|{tst}|{transition}|{region}"
    dedupe = "ot|" + hashlib.sha256(key_src.encode()).hexdigest()

    return NormalizedEvent(
        client_type="owntracks",
        dedupe_key=dedupe,
        region=region,
        transition=transition,
        event_utc=event_utc,
        lat=_f(p.get("lat")),
        lon=_f(p.get("lon")),
        acc=_f(p.get("acc")),
    )


def _normalize_shortcuts(p: dict, config: Config) -> NormalizedEvent:
    raw_region = p.get("region") or p.get("name") or ""
    region = config.canonical_region(raw_region)
    if region is None:
        raise ValueError(f"unknown region: {raw_region!r}")

    transition = _canon_transition(p.get("event") or p.get("transition") or "")

    ts = p.get("timestamp") or p.get("time") or p.get("tst")
    if ts is None:
        raise ValueError("shortcuts payload missing 'timestamp'")
    event_utc = parse_utc(ts)

    # Shortcuts CAN and SHOULD supply a per-run UUID (README explains how). If
    # present it's the idempotency key; otherwise synthesize one so a double
    # fire of the *same* crossing still dedupes.
    uuid = p.get("uuid") or p.get("id")
    if uuid:
        dedupe = f"sc|{uuid}"
    else:
        key_src = f"shortcuts|{region}|{transition}|{event_utc.isoformat()}"
        dedupe = "sc|" + hashlib.sha256(key_src.encode()).hexdigest()

    return NormalizedEvent(
        client_type="shortcuts",
        dedupe_key=dedupe,
        region=region,
        transition=transition,
        event_utc=event_utc,
        lat=_f(p.get("lat")),
        lon=_f(p.get("lon")),
        acc=_f(p.get("acc") or p.get("accuracy")),
    )

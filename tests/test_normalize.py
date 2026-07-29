"""Tests that both client formats normalize into one shape, and that
idempotency keys behave (spec: dedupe on a client-supplied UUID)."""

import pytest

from geofence.config import Config, Region
from geofence.ingest.normalize import normalize


def _config():
    regions = {
        "henry_hall": Region("henry_hall", None, None, 100, 0, ["Henry Hall", "dorm"]),
        "firestone": Region("firestone", None, None, 100, 0, ["Firestone", "library"]),
    }
    return Config(
        timezone="America/New_York",
        db_path=":memory:",
        flap_window_sec=90,
        max_transit_sec=1800,
        silence_threshold_hours=6,
        regions=regions,
        min_transit={},
        secret="x",
    )


def test_owntracks_transition_normalizes():
    payload = {
        "_type": "transition",
        "event": "leave",
        "desc": "Firestone",
        "tst": 1_772_000_000,
        "lat": 40.35,
        "lon": -74.65,
        "acc": 12,
        "tid": "jn",
    }
    ne = normalize(payload, _config())
    assert ne.client_type == "owntracks"
    assert ne.region == "firestone"
    assert ne.transition == "exit"
    assert ne.lat == 40.35
    assert ne.dedupe_key.startswith("ot|")


def test_shortcuts_normalizes_and_uses_uuid():
    payload = {
        "region": "Henry Hall",
        "event": "arrive",
        "timestamp": "2026-03-01T12:00:00Z",
        "uuid": "ABC-123",
    }
    ne = normalize(payload, _config())
    assert ne.client_type == "shortcuts"
    assert ne.region == "henry_hall"
    assert ne.transition == "enter"
    assert ne.dedupe_key == "sc|ABC-123"


def test_both_clients_produce_the_same_internal_shape():
    cfg = _config()
    ot = normalize(
        {"_type": "transition", "event": "enter", "desc": "Firestone", "tst": 1_772_000_000},
        cfg,
    )
    sc = normalize(
        {"region": "library", "event": "enter", "timestamp": 1_772_000_000, "uuid": "z"},
        cfg,
    )
    assert ot.region == sc.region == "firestone"
    assert ot.transition == sc.transition == "enter"
    assert ot.event_utc == sc.event_utc


def test_same_owntracks_crossing_yields_same_dedupe_key():
    cfg = _config()
    p = {"_type": "transition", "event": "leave", "desc": "Firestone", "tst": 1_772_000_000, "tid": "jn"}
    assert normalize(p, cfg).dedupe_key == normalize(dict(p), cfg).dedupe_key


def test_unknown_region_rejected():
    with pytest.raises(ValueError):
        normalize({"region": "mars", "event": "enter", "timestamp": 1}, _config())


def test_unknown_transition_rejected():
    with pytest.raises(ValueError):
        normalize(
            {"region": "henry_hall", "event": "teleport", "timestamp": 1}, _config()
        )


def test_alias_and_underscore_matching():
    cfg = _config()
    assert normalize({"region": "dorm", "event": "enter", "timestamp": 1}, cfg).region == "henry_hall"
    assert normalize({"region": "henry hall", "event": "enter", "timestamp": 1}, cfg).region == "henry_hall"

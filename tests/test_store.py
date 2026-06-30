"""No-repeat history: persistence and 30-day pruning."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import morning_agent.store as store
from morning_agent.store import History


def _point_store_at(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "HISTORY_PATH", tmp_path / "history.json")


def test_mark_used_and_recent(tmp_path, monkeypatch):
    _point_store_at(tmp_path, monkeypatch)
    h = History()
    assert h.recent_ids("quotes") == set()
    h.mark_used("quotes", "abc")
    assert "abc" in h.recent_ids("quotes")


def test_persistence_round_trip(tmp_path, monkeypatch):
    _point_store_at(tmp_path, monkeypatch)
    h = History()
    h.mark_used("songs", "song1")
    h.save()

    h2 = History()  # reloads from disk
    assert "song1" in h2.recent_ids("songs")


def test_prunes_entries_older_than_window(tmp_path, monkeypatch):
    _point_store_at(tmp_path, monkeypatch)
    h = History(window_days=30)
    old = (datetime.now(timezone.utc) - timedelta(days=40)).date().isoformat()
    fresh = datetime.now(timezone.utc).date().isoformat()
    # Inject directly, then save+reload to trigger prune.
    h._data["quotes"] = {"old": old, "fresh": fresh}
    h.save()

    h2 = History(window_days=30)
    recent = h2.recent_ids("quotes")
    assert "fresh" in recent
    assert "old" not in recent


def test_corrupt_file_resets_gracefully(tmp_path, monkeypatch):
    _point_store_at(tmp_path, monkeypatch)
    (tmp_path / "history.json").write_text("{ not valid json")
    h = History()  # should not raise
    assert h.recent_ids("quotes") == set()

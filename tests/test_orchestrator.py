"""The 'always sends' guarantee: a failing section never aborts the digest."""
from __future__ import annotations

import morning_agent.store as store
from morning_agent import main
from morning_agent.sections import SectionResult


def test_failing_section_degrades_but_digest_builds(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "HISTORY_PATH", tmp_path / "history.json")

    # Make the news section blow up; others use real (offline-safe) logic.
    monkeypatch.setattr(
        main.news, "build", lambda: (_ for _ in ()).throw(RuntimeError("API down"))
    )
    # Avoid network in quote; force local fallback.
    monkeypatch.setattr(main.quote, "_from_api", lambda recent: None)
    # Portfolio + suggestions need creds/network -> let them fail naturally.

    title, body, all_ok = main.build_digest()

    assert "Morning Briefing" in title
    assert all_ok is False
    assert "Couldn't fetch news" in body
    # Quote and song still rendered.
    assert "### Quote" in body and "### Song" in body
    # Footer lists failures.
    assert "section(s) failed" in body


def test_section_result_failed_helper():
    r = SectionResult.failed("News", "boom")
    assert r.ok is False
    assert "couldn't fetch news" in r.body.lower()
    assert "boom" in r.body

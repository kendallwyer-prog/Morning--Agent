"""Pure-logic tests for the content sections (no network)."""
from __future__ import annotations

import morning_agent.store as store
from morning_agent.sections import news, quote, song, suggestions
from morning_agent.store import History


def _history(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "HISTORY_PATH", tmp_path / "history.json")
    return History()


# --- quote ---------------------------------------------------------------
def test_quote_local_fallback_when_api_unavailable(tmp_path, monkeypatch):
    h = _history(tmp_path, monkeypatch)
    monkeypatch.setattr(quote, "_from_api", lambda recent: None)
    res = quote.build(h)
    assert res.ok
    assert "—" in res.body
    assert len(h.recent_ids("quotes")) == 1


def test_quote_skips_recent(tmp_path, monkeypatch):
    h = _history(tmp_path, monkeypatch)
    monkeypatch.setattr(quote, "_from_api", lambda recent: None)
    first = quote.build(h).body
    second = quote.build(h).body
    assert first != second  # no immediate repeat


# --- song ----------------------------------------------------------------
def test_song_no_repeat_across_picks(tmp_path, monkeypatch):
    h = _history(tmp_path, monkeypatch)
    picks = {song.build(h).body for _ in range(5)}
    assert len(picks) == 5  # five distinct songs


# --- news ----------------------------------------------------------------
def test_news_interleaves_dedupes_and_caps(monkeypatch):
    monkeypatch.setenv("GNEWS_API_KEY", "x")
    monkeypatch.setattr(news.config, "NEWS_CATEGORIES", ["world", "business", "technology"])

    def fake_fetch(api_key, category):
        return {
            "world": [
                {"title": "W1", "source": {"name": "R"}},
                {"title": "Shared", "source": {"name": "A"}},
            ],
            "business": [
                {"title": "B1", "source": {"name": "B"}},
                {"title": "Shared", "source": {"name": "Dup"}},
            ],
            "technology": [
                {"title": "T1", "source": {"name": "V"}},
                {"title": "T2", "source": {"name": "TC"}},
            ],
        }.get(category, [])

    monkeypatch.setattr(news, "_fetch_category", fake_fetch)
    res = news.build()
    assert res.ok
    lines = [ln for ln in res.body.splitlines() if ln.startswith("•")]
    assert len(lines) == 4  # capped
    assert sum("Shared" in ln for ln in lines) == 1  # deduped
    # first three span the three categories (round-robin)
    assert lines[0].startswith("• W1")
    assert lines[1].startswith("• B1")
    assert lines[2].startswith("• T1")


def test_news_tolerates_partial_category_failure(monkeypatch):
    monkeypatch.setenv("GNEWS_API_KEY", "x")
    monkeypatch.setattr(news.config, "NEWS_CATEGORIES", ["world", "business"])
    import requests

    def fake_fetch(api_key, category):
        if category == "business":
            raise requests.RequestException("boom")
        return [{"title": "W1", "source": {"name": "R"}}]

    monkeypatch.setattr(news, "_fetch_category", fake_fetch)
    res = news.build()
    assert res.ok
    assert "W1" in res.body
    assert "unavailable" in res.body


# --- suggestions ---------------------------------------------------------
def test_suggestions_universe_activates_held_sectors():
    holdings = [{"symbol": "MSFT", "quantity": 5}, {"symbol": "JPM", "quantity": 2}]
    cands, ctx = suggestions._candidate_universe(holdings)
    assert "MSFT" not in cands  # held, excluded
    assert "AAPL" in cands and "Technology" in ctx["AAPL"]
    assert "V" in cands and "Financial Services" in ctx["V"]


def test_suggestions_watchlist_fallback_for_etf_only():
    cands, ctx = suggestions._candidate_universe([{"symbol": "VTI", "quantity": 10}])
    assert cands  # non-empty
    assert all("watchlist" in ctx[c] for c in cands)


def test_suggestions_metrics_and_ranking():
    rising = [100, 101, 102, 103, 104, 105, 108]
    falling = [200, 198, 196, 194, 190, 188, 185]
    assert suggestions._metrics_from_closes([1, 2, 3]) is None  # too short
    metrics = {
        "UP": suggestions._metrics_from_closes(rising),
        "DOWN": suggestions._metrics_from_closes(falling),
    }
    assert metrics["UP"]["mom5"] > 0
    assert metrics["DOWN"]["mom5"] < 0
    ranked = suggestions.rank(metrics, {"UP": "ctx", "DOWN": "ctx"})
    assert ranked[0][0] == "UP"  # higher momentum first

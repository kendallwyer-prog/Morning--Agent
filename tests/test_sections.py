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
_RSS_SAMPLE = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Feed Title</title>
  <item><title>Feed Title</title></item>
  <item><title>Real headline one</title></item>
  <item><title>Some story - Reuters</title></item>
  <item><title>Extra story three</title></item>
</channel></rss>"""


def test_news_parse_titles_cleans_and_limits():
    titles = news._parse_titles(_RSS_SAMPLE, limit=2)
    # Channel-title boilerplate item skipped; " - Publisher" suffix trimmed.
    assert titles == ["Real headline one", "Some story"]


def test_news_analysis_finds_shared_leads_and_unique():
    by_source = {
        "AJ": ["Gaza ceasefire talks resume", "Bengal school lunch debate"],
        "BBC": ["Gaza aid convoy blocked", "Starmer defence spending row"],
    }
    text = news._analysis(by_source)
    assert "gaza" in text.lower()          # shared theme across both
    assert "Shared themes" in text
    assert "Leads" in text
    # each source's lead headline appears
    assert "Gaza ceasefire talks resume" in text
    assert "Gaza aid convoy blocked" in text


def test_news_build_groups_by_source_and_analyses(monkeypatch):
    monkeypatch.setattr(
        news, "_fetch_source",
        lambda url, limit: {
            "u1": ["Gaza ceasefire talks", "Local election result"],
            "u2": ["Gaza aid blocked", "Market rally continues"],
        }[url],
    )
    monkeypatch.setattr(
        news.Path, "read_text",
        lambda self: '{"per_source":2,"sources":[{"name":"AJ","url":"u1"},{"name":"BBC","url":"u2"}]}',
    )
    res = news.build()
    assert res.ok
    assert "▸ AJ" in res.body and "▸ BBC" in res.body
    assert "Gaza ceasefire talks" in res.body
    assert "📊 Compare" in res.body


def test_news_tolerates_source_failure(monkeypatch):
    import requests

    def fake_fetch(url, limit):
        if url == "bad":
            raise requests.RequestException("boom")
        return ["Working headline"]

    monkeypatch.setattr(news, "_fetch_source", fake_fetch)
    monkeypatch.setattr(
        news.Path, "read_text",
        lambda self: '{"per_source":2,"sources":[{"name":"Good","url":"ok"},{"name":"Bad","url":"bad"}]}',
    )
    res = news.build()
    assert res.ok
    assert "Working headline" in res.body
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

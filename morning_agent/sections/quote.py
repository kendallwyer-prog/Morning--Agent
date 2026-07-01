"""Quote section.

Strategy:
1. Try the free Quotable API (no key) a few times, skipping any quote we've
   shown in the last 30 days.
2. If the API is unreachable or only returns repeats, fall back to the curated
   local list (data/quotes.json), again skipping recent picks.
3. Record the chosen quote in the no-repeat history.

A stable id (hash of "author|text") is used so the same quote is recognised
whether it came from the API or the local list.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import requests

from . import SectionResult

_BUCKET = "quotes"
_DATA = Path(__file__).resolve().parent.parent.parent / "data" / "quotes.json"
_QUOTABLE_URL = "https://api.quotable.io/random"
_API_ATTEMPTS = 4
_TIMEOUT = 8


def _quote_id(author: str, text: str) -> str:
    norm = f"{author.strip().lower()}|{text.strip().lower()}"
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def _render(text: str, author: str) -> str:
    return f"“{text}”\n— {author}"


def _from_api(recent: set[str]) -> tuple[str, str, str] | None:
    """Return (id, text, author) from Quotable, avoiding recent ids."""
    for _ in range(_API_ATTEMPTS):
        try:
            resp = requests.get(
                _QUOTABLE_URL,
                # Philosophy-specific: Quotable tags (| = OR).
                params={"maxLength": 200, "tags": "philosophy|wisdom"},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            text = (data.get("content") or "").strip()
            author = (data.get("author") or "Unknown").strip()
            if not text:
                continue
            qid = _quote_id(author, text)
            if qid not in recent:
                return qid, text, author
        except (requests.RequestException, ValueError):
            return None  # network/JSON failure -> let caller use local list
    return None  # only repeats came back


def _from_local(recent: set[str]) -> tuple[str, str, str] | None:
    quotes = json.loads(_DATA.read_text())
    # Prefer the first non-recent quote; if all are recent, reuse the oldest
    # by falling through to the first entry.
    for q in quotes:
        qid = _quote_id(q["author"], q["text"])
        if qid not in recent:
            return qid, q["text"], q["author"]
    if quotes:
        q = quotes[0]
        return _quote_id(q["author"], q["text"]), q["text"], q["author"]
    return None


def build(history=None) -> SectionResult:
    recent = history.recent_ids(_BUCKET) if history else set()

    chosen = _from_api(recent) or _from_local(recent)
    if chosen is None:
        return SectionResult.failed("Quote", "no quotes available")

    qid, text, author = chosen
    if history:
        history.mark_used(_BUCKET, qid)
    return SectionResult(title="Quote", body=_render(text, author))

"""News section — top headlines via GNews (https://gnews.io).

Free tier: 100 requests/day, up to 10 articles per request. We make one
top-headlines request per configured category (default: world, business,
technology), interleave the results, dedupe by title, and present the top 3-4
as concise "headline — source" lines.

We show real headlines rather than a synthesized prose summary so nothing is
fabricated. Each headline is trimmed and the source attributed.

Requires GNEWS_API_KEY. If it's missing or the API errors, the section raises
and the orchestrator turns it into a graceful "couldn't fetch news" note.
"""
from __future__ import annotations

import requests

from .. import config
from . import SectionResult

_URL = "https://gnews.io/api/v4/top-headlines"
_TIMEOUT = 12
_MAX_HEADLINES = 4
_PER_CATEGORY = 2

# GNews recognises these categories; we map our friendly names onto them.
_CATEGORY_MAP = {
    "world": "world",
    "business": "business",
    "technology": "technology",
    "tech": "technology",
    "nation": "nation",
    "science": "science",
    "health": "health",
    "sports": "sports",
    "entertainment": "entertainment",
    "general": "general",
}


def _fetch_category(api_key: str, category: str) -> list[dict]:
    resp = requests.get(
        _URL,
        params={
            "category": category,
            "lang": config.NEWS_LANG,
            "country": config.NEWS_COUNTRY,
            "max": _PER_CATEGORY,
            "apikey": api_key,
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("articles", []) or []


def _interleave(per_category: list[list[dict]]) -> list[dict]:
    """Round-robin so the final list spans categories rather than front-loading
    one of them."""
    out: list[dict] = []
    for i in range(_PER_CATEGORY):
        for articles in per_category:
            if i < len(articles):
                out.append(articles[i])
    return out


def build() -> SectionResult:
    api_key = config.require("GNEWS_API_KEY")

    categories = []
    for name in config.NEWS_CATEGORIES:
        mapped = _CATEGORY_MAP.get(name.lower())
        if mapped and mapped not in categories:
            categories.append(mapped)
    if not categories:
        categories = ["world", "business", "technology"]

    per_category: list[list[dict]] = []
    errors = 0
    for cat in categories:
        try:
            per_category.append(_fetch_category(api_key, cat))
        except requests.RequestException:
            errors += 1
            per_category.append([])

    articles = _interleave(per_category)

    # Dedupe by normalised title, keep order, cap at the limit.
    seen: set[str] = set()
    picked: list[dict] = []
    for a in articles:
        title = (a.get("title") or "").strip()
        if not title:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        picked.append(a)
        if len(picked) >= _MAX_HEADLINES:
            break

    if not picked:
        # Every category failed or returned nothing.
        raise RuntimeError("no headlines returned")

    lines = []
    for a in picked:
        title = a["title"].strip()
        source = (a.get("source") or {}).get("name", "").strip()
        lines.append(f"• {title}" + (f" — *{source}*" if source else ""))

    body = "\n".join(lines)
    if errors:
        body += f"\n\n_({errors} categor{'y' if errors == 1 else 'ies'} unavailable)_"
    return SectionResult(title="News", body=body)

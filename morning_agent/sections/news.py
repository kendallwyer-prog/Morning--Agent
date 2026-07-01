"""News section — headlines grouped by source, plus a comparative analysis.

Reads a curated list of outlets from data/news_feeds.json (Al Jazeera, The
Economist, BBC UK, WSJ, Global Times by default — all free RSS, no API key),
shows the top few headlines from each, then appends a transparent, heuristic
analysis comparing what the outlets lead with and where they overlap or differ.

The analysis is intentionally explainable (shared keywords, lead stories, unique
angles) — no black box. Individual source failures are tolerated; a total
failure degrades to a graceful "couldn't fetch news" note.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

from . import SectionResult

_FEEDS_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "news_feeds.json"
_TIMEOUT = 15
_HEADERS = {"User-Agent": "Mozilla/5.0 (morning-agent)"}

# Common words to ignore when finding shared/unique topics.
_STOP = {
    "the", "a", "an", "and", "or", "but", "for", "to", "of", "in", "on", "at",
    "by", "with", "from", "as", "is", "are", "was", "were", "be", "been", "will",
    "has", "have", "had", "not", "no", "new", "over", "after", "before", "into",
    "amid", "says", "say", "said", "how", "why", "what", "who", "when", "this",
    "that", "these", "those", "up", "down", "out", "off", "more", "most", "than",
    "its", "it", "his", "her", "their", "you", "your", "our", "we", "they",
    "he", "she", "him", "them", "us", "may", "can", "could", "would", "should",
    "one", "two", "first", "year", "years", "day", "week", "day", "day",
    "video", "news", "live", "latest", "top", "world", "uk",
}


def _tag(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1].lower()


def _find_child_text(item: ET.Element, name: str) -> str:
    for child in item:
        if _tag(child) == name:
            return (child.text or "").strip()
    return ""


def _parse_titles(xml_bytes: bytes, limit: int) -> list[str]:
    """Return up to `limit` cleaned item/entry titles from an RSS/Atom feed."""
    root = ET.fromstring(xml_bytes)
    channel_title = ""
    for el in root.iter():
        if _tag(el) in ("channel", "feed"):
            channel_title = _find_child_text(el, "title")
            break

    titles: list[str] = []
    for el in root.iter():
        if _tag(el) not in ("item", "entry"):
            continue
        title = _find_child_text(el, "title")
        if not title:
            continue
        # Skip boilerplate "channel title" items some feeds emit as item 0.
        if channel_title and title.strip() == channel_title.strip():
            continue
        # Google-News-style "Headline - Publisher" trimming.
        title = re.sub(r"\s+-\s+[^-]{2,40}$", "", title).strip()
        if title and title not in titles:
            titles.append(title)
        if len(titles) >= limit:
            break
    return titles


def _fetch_source(url: str, limit: int) -> list[str]:
    resp = requests.get(url, timeout=_TIMEOUT, headers=_HEADERS)
    resp.raise_for_status()
    return _parse_titles(resp.content, limit)


def _keywords(title: str) -> set[str]:
    words = re.findall(r"[A-Za-z][A-Za-z'-]{3,}", title.lower())
    return {w for w in words if w not in _STOP}


def _analysis(by_source: dict[str, list[str]]) -> str:
    """Transparent comparison: shared themes, each outlet's lead, unique angles."""
    sources = [s for s, ts in by_source.items() if ts]
    if len(sources) < 2:
        return ""

    # term -> set of sources mentioning it
    term_sources: dict[str, set[str]] = {}
    # source -> multiset of terms
    source_terms: dict[str, set[str]] = {}
    for src in sources:
        st: set[str] = set()
        for title in by_source[src]:
            kws = _keywords(title)
            st |= kws
            for kw in kws:
                term_sources.setdefault(kw, set()).add(src)
        source_terms[src] = st

    # Shared themes: terms appearing in the most sources (>=2).
    shared = sorted(
        ((t, len(s)) for t, s in term_sources.items() if len(s) >= 2),
        key=lambda x: (-x[1], x[0]),
    )[:4]

    lines: list[str] = []
    if shared:
        pretty = ", ".join(f"{t} ({n}/{len(sources)})" for t, n in shared)
        lines.append(f"**Shared themes:** {pretty}.")
    else:
        lines.append("**Shared themes:** none obvious — the outlets diverge today.")

    # Each outlet's lead story.
    leads = []
    for src in sources:
        lead = by_source[src][0]
        leads.append(f"_{src}:_ {lead}")
    lines.append("**Leads —** " + " · ".join(leads))

    # Unique angle per source: a term only it uses (most distinctive).
    uniques = []
    for src in sources:
        only = [t for t in source_terms[src] if term_sources[t] == {src}]
        if only:
            only.sort()
            uniques.append(f"{src} → {only[0]}")
    if uniques:
        lines.append("**Distinct angles:** " + "; ".join(uniques) + ".")

    return "\n".join(lines)


def build() -> SectionResult:
    cfg = json.loads(_FEEDS_FILE.read_text())
    per_source = int(cfg.get("per_source", 3))
    sources = cfg.get("sources", [])
    if not sources:
        raise RuntimeError("no sources configured")

    by_source: dict[str, list[str]] = {}
    errors: list[str] = []
    for src in sources:
        name = src.get("name", "?")
        try:
            titles = _fetch_source(src["url"], per_source)
            by_source[name] = titles
            if not titles:
                errors.append(name)
        except (requests.RequestException, ET.ParseError, KeyError):
            by_source[name] = []
            errors.append(name)

    if not any(by_source.values()):
        raise RuntimeError("all sources unavailable")

    blocks: list[str] = []
    for name, titles in by_source.items():
        if titles:
            bullets = "\n".join(f"• {t}" for t in titles)
            blocks.append(f"**{name}**\n{bullets}")
        else:
            blocks.append(f"**{name}**\n_unavailable_")

    body = "\n\n".join(blocks)

    analysis = _analysis(by_source)
    if analysis:
        body += "\n\n**📊 Compare**\n" + analysis

    if errors:
        body += f"\n\n_({len(errors)} source(s) unavailable: {', '.join(errors)})_"

    return SectionResult(title="News", body=body)

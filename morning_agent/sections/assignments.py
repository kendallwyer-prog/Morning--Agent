"""Academic assignments section.

Reads a local list of assignments (data/assignments.json), keeps the ones that
are still outstanding (``done`` is false), and surfaces the soonest deadlines
so nothing sneaks up on you. Everything is local — no API, no key, never breaks
on network — which fits the digest's "always sends" contract.

Each assignment is:
    {
      "title": "Problem Set 3",
      "course": "MATH140",
      "type": "homework",
      "due": "2026-09-04",   # ISO date (YYYY-MM-DD)
      "notes": "",
      "done": false,
      "id": "93f3aeab6c"
    }

Rendering:
- Overdue items are always shown first, loudly, so a missed deadline can't hide.
- Then the soonest upcoming items, capped at ``ASSIGNMENTS_MAX`` (default 6).
- An urgency marker is derived from days-until-due; the due date is computed in
  the digest's configured timezone so "today"/"tomorrow" line up with the user.
- Malformed entries (missing/invalid ``due``) are skipped rather than crashing.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import config
from . import SectionResult

_DATA = Path(__file__).resolve().parent.parent.parent / "data" / "assignments.json"

# Type -> emoji. Unknown types fall back to a neutral marker.
_TYPE_EMOJI = {
    "reading": "📖",
    "homework": "✏️",
    "exam": "📝",
    "quiz": "❓",
    "paper": "📄",
    "essay": "📄",
    "project": "🧩",
    "lab": "🔬",
    "presentation": "🎤",
    "discussion": "💬",
}
_DEFAULT_TYPE_EMOJI = "📌"


def _today() -> date:
    """Today's date in the digest's configured timezone."""
    return datetime.now(ZoneInfo(config.TIMEZONE)).date()


def _parse_due(value) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _urgency(days_left: int) -> str:
    """A short human phrase + coloured dot for the days-until-due."""
    if days_left < 0:
        n = -days_left
        return f"🔴 overdue by {n} day{'s' if n != 1 else ''}"
    if days_left == 0:
        return "🔴 due today"
    if days_left == 1:
        return "🟠 due tomorrow"
    if days_left <= 3:
        return f"🟠 in {days_left} days"
    if days_left <= 7:
        return f"🟡 in {days_left} days"
    return f"📅 in {days_left} days"


def _render_line(a: dict, days_left: int) -> str:
    emoji = _TYPE_EMOJI.get(str(a.get("type", "")).lower(), _DEFAULT_TYPE_EMOJI)
    title = str(a.get("title") or "Untitled").strip()
    course = str(a.get("course") or "").strip()
    due = a["_due"].strftime("%a %b %-d")

    head = f"{emoji} **{title}**"
    if course:
        head += f" · {course}"
    line = f"{head} — {due} ({_urgency(days_left)})"

    notes = str(a.get("notes") or "").strip()
    if notes:
        line += f"\n  _{notes}_"
    return line


def build() -> SectionResult:
    try:
        raw = json.loads(_DATA.read_text())
    except FileNotFoundError:
        return SectionResult(title="Assignments", body="_No assignments file yet._")

    if not isinstance(raw, list):
        raise RuntimeError("assignments.json must contain a list")

    today = _today()

    pending: list[dict] = []
    for a in raw:
        if not isinstance(a, dict) or a.get("done"):
            continue
        due = _parse_due(a.get("due"))
        if due is None:
            continue
        a = {**a, "_due": due}
        pending.append(a)

    if not pending:
        return SectionResult(
            title="Assignments", body="🎉 Nothing outstanding — you're all caught up."
        )

    pending.sort(key=lambda a: a["_due"])

    max_items = config.ASSIGNMENTS_MAX
    # Overdue items are always shown; the cap only trims the upcoming tail.
    overdue = [a for a in pending if (a["_due"] - today).days < 0]
    upcoming = [a for a in pending if (a["_due"] - today).days >= 0]
    shown = overdue + upcoming[:max_items]
    hidden = len(pending) - len(shown)

    lines = [_render_line(a, (a["_due"] - today).days) for a in shown]
    body = "\n".join(lines)
    if hidden > 0:
        body += f"\n\n_(+{hidden} more upcoming)_"
    return SectionResult(title="Assignments", body=body)

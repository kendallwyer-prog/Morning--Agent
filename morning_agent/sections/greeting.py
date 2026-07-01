"""Greeting section — "hello" and "good morning" in a rotating language.

Picks a language from data/greetings.json, avoiding any used in the last 30
days (same no-repeat logic as quotes/songs). Shows the native phrase plus a
romanization for non-Latin scripts.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import SectionResult

_BUCKET = "greetings"
_DATA = Path(__file__).resolve().parent.parent.parent / "data" / "greetings.json"


def _phrase(text: str, rom: str | None) -> str:
    return f"{text} ({rom})" if rom else text


def _render(g: dict) -> str:
    hello = _phrase(g["hello"], g.get("hello_rom"))
    morning = _phrase(g["morning"], g.get("morning_rom"))
    return f"{g['language']}\nHello — {hello}\nGood morning — {morning}"


def build(history=None) -> SectionResult:
    greetings = json.loads(_DATA.read_text())
    if not greetings:
        return SectionResult.failed("Greeting", "no greetings available")

    recent = history.recent_ids(_BUCKET) if history else set()

    chosen = None
    for g in greetings:
        if g["language"].lower() not in recent:
            chosen = g
            break
    if chosen is None:
        chosen = greetings[0]

    if history:
        history.mark_used(_BUCKET, chosen["language"].lower())
    return SectionResult(title="Greeting", body=_render(chosen))

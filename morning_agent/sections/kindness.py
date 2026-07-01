"""Kindness section — one tangible act of kindness to do today.

Rotates through data/kindness.json with 30-day no-repeat logic. The id is a
stable hash of the text so re-ordering the list doesn't reshuffle history.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import SectionResult

_BUCKET = "kindness"
_DATA = Path(__file__).resolve().parent.parent.parent / "data" / "kindness.json"


def _act_id(text: str) -> str:
    return hashlib.sha1(text.strip().lower().encode("utf-8")).hexdigest()[:12]


def build(history=None) -> SectionResult:
    acts = json.loads(_DATA.read_text())
    if not acts:
        return SectionResult.failed("Kindness", "no acts available")

    recent = history.recent_ids(_BUCKET) if history else set()

    chosen = None
    for act in acts:
        if _act_id(act) not in recent:
            chosen = act
            break
    if chosen is None:
        chosen = acts[0]

    if history:
        history.mark_used(_BUCKET, _act_id(chosen))
    return SectionResult(title="Kindness", body=chosen)

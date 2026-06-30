"""Quote section — STUB (Phase 2).

Will fetch from the Quotable API with a curated local fallback, applying
30-day no-repeat logic via store.History.
"""
from __future__ import annotations

from . import SectionResult


def build(history=None) -> SectionResult:
    return SectionResult(title="Quote", body="_(quote section coming in Phase 2)_")

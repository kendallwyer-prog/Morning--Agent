"""Section contract shared by every part of the digest.

Each section exposes ``build(...) -> SectionResult``. The orchestrator wraps
every call in try/except, so a section may raise freely; failure is converted
into a visible "couldn't fetch" note and the rest of the digest still sends.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SectionResult:
    title: str          # short heading, e.g. "Quote"
    body: str           # rendered markdown/text for this section
    ok: bool = True     # False => a degraded/failed section note

    @classmethod
    def failed(cls, title: str, reason: str) -> "SectionResult":
        return cls(title=title, body=f"_⚠️ Couldn't fetch {title.lower()}: {reason}_", ok=False)

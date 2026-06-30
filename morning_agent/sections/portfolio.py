"""Portfolio section — STUB (Phase 4).

Will log into Robinhood via robin_stocks (TOTP 2FA through pyotp) and report
total value, day's gain/loss, and per-position breakdown. Failures fall back to
a "couldn't fetch portfolio" note rather than crashing the digest.
"""
from __future__ import annotations

from . import SectionResult


def build() -> SectionResult:
    return SectionResult(title="Portfolio", body="_(portfolio section coming in Phase 4)_")

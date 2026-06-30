"""Stock suggestions section — STUB (Phase 5).

Will use transparent, explainable rules (momentum within sectors you already
hold, basic screening via yfinance) to surface 1-3 ideas, each labelled
"not financial advice — for informational purposes only".
"""
from __future__ import annotations

from . import SectionResult


def build(holdings=None) -> SectionResult:
    return SectionResult(title="Ideas", body="_(stock suggestions coming in Phase 5)_")

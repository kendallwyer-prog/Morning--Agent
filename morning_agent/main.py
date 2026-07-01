"""Orchestrator: build the morning digest and push it.

Reliability contract:
- Every section runs inside its own try/except. A raised exception becomes a
  visible "couldn't fetch" note; it never aborts the run.
- The notification is ALWAYS sent as long as delivery itself works, even if
  every content section failed.
- No-repeat state is saved only after a successful build so a crash doesn't burn
  a quote/song.

Run locally:  python -m morning_agent.main
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config
from .delivery import notify
from .sections import SectionResult, news, portfolio, quote, song, suggestions
from .store import History


def _safe(title: str, fn) -> SectionResult:
    """Run a section builder, converting any failure into a degraded result."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - we deliberately catch everything
        traceback.print_exc()
        return SectionResult.failed(title, str(exc) or exc.__class__.__name__)


def build_digest() -> tuple[str, str, bool]:
    """Return (title, markdown_body, all_ok)."""
    history = History()

    # Portfolio is fetched first so suggestions can reference held positions.
    portfolio_result = _safe("Portfolio", portfolio.build)
    holdings = getattr(portfolio_result, "holdings", None)

    sections = [
        _safe("Quote", lambda: quote.build(history)),
        _safe("Song", lambda: song.build(history)),
        _safe("News", news.build),
        portfolio_result,
        _safe("Ideas", lambda: suggestions.build(holdings)),
    ]

    tz = ZoneInfo(config.TIMEZONE)
    now = datetime.now(tz)
    date_line = now.strftime("%A, %B %-d")

    emoji = {
        "Quote": "💭", "Song": "🎵", "News": "📰",
        "Portfolio": "💼", "Ideas": "💡",
    }
    parts: list[str] = []
    for s in sections:
        head = f"{emoji.get(s.title, '•')}  {s.title.upper()}"
        parts.append(f"{head}\n{s.body}")
    body = "\n\n\n".join(parts)

    failed = [s.title for s in sections if not s.ok]
    if failed:
        body += "\n\n" + "─" * 20
        body += f"\n⚠️ {len(failed)} section(s) unavailable: {', '.join(failed)}"

    title = f"☀️ Morning Briefing · {date_line}"

    # Persist no-repeat state now that the digest is assembled.
    try:
        history.save()
    except Exception:  # noqa: BLE001
        traceback.print_exc()

    return title, body, not failed


def main() -> int:
    title, body, all_ok = build_digest()
    try:
        notify.send(title, body, priority=4, tags=["sunrise"])
    except Exception as exc:  # noqa: BLE001
        # Delivery itself failed — surface loudly so CI marks the run failed.
        print(f"FATAL: notification delivery failed: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1
    print(f"Sent digest (all_ok={all_ok}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

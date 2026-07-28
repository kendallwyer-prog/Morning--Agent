"""Centralised configuration: load env vars once, expose typed helpers.

Nothing here raises on import — missing values are surfaced per-section so a
single missing key never takes down the whole digest. Use ``require()`` inside
a section when a value is mandatory for that section only.
"""
from __future__ import annotations

import os

try:
    # Local dev convenience; a no-op when python-dotenv isn't installed
    # or when running in CI where vars come from the environment directly.
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional at runtime
    pass


def get(name: str, default: str | None = None) -> str | None:
    """Return an env var, treating empty/whitespace as unset."""
    val = os.environ.get(name)
    if val is None:
        return default
    val = val.strip()
    return val if val else default


def require(name: str) -> str:
    """Return an env var or raise a clear error (caught per-section)."""
    val = get(name)
    if not val:
        raise RuntimeError(f"Required environment variable {name!r} is not set")
    return val


def get_list(name: str, default: list[str] | None = None) -> list[str]:
    """Parse a comma-separated env var into a clean list."""
    raw = get(name)
    if not raw:
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


# --- Delivery -------------------------------------------------------------
NTFY_SERVER = get("NTFY_SERVER", "https://ntfy.sh")

# --- News -----------------------------------------------------------------
NEWS_CATEGORIES = get_list("NEWS_CATEGORIES", ["world", "business", "technology"])
NEWS_COUNTRY = get("NEWS_COUNTRY", "us")
NEWS_LANG = get("NEWS_LANG", "en")

# --- Assignments ----------------------------------------------------------
def get_int(name: str, default: int) -> int:
    """Parse an integer env var, falling back to ``default`` on any problem."""
    raw = get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# How many upcoming (non-overdue) assignments to list. Overdue items are always
# shown in full regardless of this cap.
ASSIGNMENTS_MAX = get_int("ASSIGNMENTS_MAX", 6)

# --- Misc -----------------------------------------------------------------
TIMEZONE = get("TIMEZONE", "America/New_York")

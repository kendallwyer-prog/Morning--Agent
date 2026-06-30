"""Tiny JSON-backed state store for no-repeat logic.

Tracks recently-used quote and song identifiers with timestamps so we can avoid
repeats within a rolling window (default 30 days). The file lives at
``data/history.json`` and is committed back to the repo by the GitHub Action so
state survives between ephemeral CI runs.

Design notes:
- Stored as {"quotes": {id: iso_date}, "songs": {id: iso_date}}.
- We prune entries older than the window on every load so the file stays small.
- All operations are best-effort: a corrupt/missing file resets to empty
  rather than crashing the digest.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
HISTORY_PATH = DATA_DIR / "history.json"
DEFAULT_WINDOW_DAYS = 30


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today_iso() -> str:
    return _now().date().isoformat()


class History:
    """Rolling no-repeat history for a given window."""

    def __init__(self, window_days: int = DEFAULT_WINDOW_DAYS) -> None:
        self.window_days = window_days
        self._data: dict[str, dict[str, str]] = {"quotes": {}, "songs": {}}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(HISTORY_PATH.read_text())
            for bucket in ("quotes", "songs"):
                if isinstance(raw.get(bucket), dict):
                    self._data[bucket] = raw[bucket]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            # Fresh start — empty history is fine.
            pass
        self._prune()

    def _prune(self) -> None:
        cutoff = (_now() - timedelta(days=self.window_days)).date().isoformat()
        for bucket in self._data:
            self._data[bucket] = {
                k: v for k, v in self._data[bucket].items() if v >= cutoff
            }

    def recent_ids(self, bucket: str) -> set[str]:
        """IDs used within the window — candidates to avoid."""
        return set(self._data.get(bucket, {}).keys())

    def mark_used(self, bucket: str, item_id: str) -> None:
        self._data.setdefault(bucket, {})[item_id] = _today_iso()

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._prune()
        HISTORY_PATH.write_text(json.dumps(self._data, indent=2, sort_keys=True))

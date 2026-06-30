"""Song section.

Picks from a curated list (data/songs.json) using the same 30-day no-repeat
logic as quotes. Spotify's recommendation API was deprecated for new apps, so a
curated rotating list is the transparent, zero-auth, never-breaks choice.

The id is a hash of "artist|track" so re-ordering the JSON doesn't reshuffle
history.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import SectionResult

_BUCKET = "songs"
_DATA = Path(__file__).resolve().parent.parent.parent / "data" / "songs.json"


def _song_id(artist: str, track: str) -> str:
    norm = f"{artist.strip().lower()}|{track.strip().lower()}"
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def _render(song: dict) -> str:
    return f"🎵 **{song['track']}** — {song['artist']}\n_{song['reason']}_"


def build(history=None) -> SectionResult:
    songs = json.loads(_DATA.read_text())
    if not songs:
        return SectionResult.failed("Song", "no songs available")

    recent = history.recent_ids(_BUCKET) if history else set()

    chosen = None
    for song in songs:
        if _song_id(song["artist"], song["track"]) not in recent:
            chosen = song
            break
    if chosen is None:
        # Everything used recently — fall back to the first entry.
        chosen = songs[0]

    if history:
        history.mark_used(_BUCKET, _song_id(chosen["artist"], chosen["track"]))
    return SectionResult(title="Song", body=_render(chosen))

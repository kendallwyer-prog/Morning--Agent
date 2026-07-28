"""Geofence event ingestion & logging layer (Phase 1).

Captures raw geofence crossings from an iPhone (Shortcuts) or OwnTracks,
stores them immutably, and derives travel/dwell segments from them.

Phase 1 is ingestion + logging only: no alerting, no advice, no scheduling math.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"

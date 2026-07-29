"""Configuration loading.

All tunables live in ``geofence.toml`` so nothing in the code is a magic
number. The shared secret is the one thing that is NEVER in the toml -- it
comes from the ``GEOFENCE_SECRET`` environment variable (set via the systemd
EnvironmentFile), so it is never committed.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field


@dataclass
class Region:
    id: str
    lat: float | None
    lon: float | None
    radius_m: float
    # Per-region calibration offset in seconds. iOS fires "leave" LATE, so the
    # true departure = recorded_exit - offset. Positive value => recorded exit
    # is that many seconds later than reality. Estimate it with `calibrate`.
    calibration_offset_sec: int = 0
    aliases: list[str] = field(default_factory=list)


@dataclass
class Config:
    timezone: str
    db_path: str
    # Boundary-flap suppression: an exit within this many seconds of an enter
    # for the SAME region is discarded as flapping noise.
    flap_window_sec: int
    # Ceiling on a plausible transit. An exit whose next arrival is further away
    # than this in time has no plausible match -> the segment is `incomplete`
    # and excluded from stats (this is the "one bad day" / four-hour-walk guard).
    max_transit_sec: int
    # `doctor` warns if no raw event has arrived in this many hours.
    silence_threshold_hours: float
    regions: dict[str, Region]
    # Per-pair minimum plausible transit seconds, keyed "from->to" (e.g.
    # "henry_hall->firestone"). A crossing faster than this is flagged
    # `suspect` -- for regions whose geofences overlap (henry_hall/firestone).
    min_transit: dict[str, int]
    # Never stored in the toml. None when unset (CLI stats/doctor don't need it;
    # the API refuses to start without it).
    secret: str | None

    def region_offset(self, region_id: str) -> int:
        r = self.regions.get(region_id)
        return r.calibration_offset_sec if r else 0

    def canonical_region(self, raw: str | None) -> str | None:
        """Map an incoming region label (e.g. OwnTracks 'Henry Hall') to a
        canonical region id (e.g. 'henry_hall'), or None if unknown."""
        if not raw:
            return None
        key = str(raw).strip().lower()
        key_u = key.replace(" ", "_")
        for rid, r in self.regions.items():
            names = {rid.lower(), rid.replace("_", " ").lower()}
            names.update(a.lower() for a in r.aliases)
            names_u = {n.replace(" ", "_") for n in names}
            if key in names or key_u in names_u:
                return rid
        return None


def load_config(path: str | None = None) -> Config:
    path = path or os.environ.get("GEOFENCE_CONFIG", "geofence.toml")
    with open(path, "rb") as f:
        data = tomllib.load(f)

    regions: dict[str, Region] = {}
    for rid, r in data.get("regions", {}).items():
        regions[rid] = Region(
            id=rid,
            lat=r.get("lat"),
            lon=r.get("lon"),
            radius_m=float(r.get("radius_m", 100)),
            calibration_offset_sec=int(r.get("calibration_offset_sec", 0)),
            aliases=list(r.get("aliases", [])),
        )

    th = data.get("thresholds", {})
    return Config(
        timezone=data.get("timezone", "America/New_York"),
        db_path=data.get("db_path", "data/geofence.db"),
        flap_window_sec=int(th.get("flap_window_sec", 90)),
        max_transit_sec=int(th.get("max_transit_sec", 1800)),
        silence_threshold_hours=float(th.get("silence_threshold_hours", 6)),
        regions=regions,
        min_transit={k: int(v) for k, v in data.get("min_transit", {}).items()},
        secret=os.environ.get("GEOFENCE_SECRET"),
    )

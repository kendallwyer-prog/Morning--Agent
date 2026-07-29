"""`calibrate`: estimate the per-region departure bias.

iOS fires "leave" late -- by 20-60s and 100+ metres. During week one you type
your ACTUAL departure times as ground truth; this module then matches each
ground-truth departure to the nearest recorded `exit` event for that region and
reports the median gap. That median is your suggested
``calibration_offset_sec`` to paste into geofence.toml (positive => iOS fired
that many seconds late).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Config
from .ingest.timeutil import iso_utc, parse_iso, parse_utc

# Only match a ground-truth departure to an exit within this window; beyond it,
# they're probably unrelated events.
_MATCH_WINDOW_SEC = 30 * 60


def add_ground_truth(
    conn: sqlite3.Connection, config: Config, region_raw: str, when: str, note: str | None
) -> str:
    region = config.canonical_region(region_raw)
    if region is None:
        raise ValueError(f"unknown region: {region_raw!r}")
    when_utc = parse_utc(when)
    conn.execute(
        "INSERT INTO ground_truth (region, actual_depart_utc, note, created_at_utc) "
        "VALUES (?, ?, ?, ?)",
        (region, iso_utc(when_utc), note, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return region


@dataclass
class RegionEstimate:
    region: str
    n: int
    median_offset_sec: float | None
    samples_sec: list[float]


def estimate(conn: sqlite3.Connection, config: Config) -> list[RegionEstimate]:
    truths = conn.execute(
        "SELECT region, actual_depart_utc FROM ground_truth ORDER BY region, actual_depart_utc"
    ).fetchall()

    by_region: dict[str, list[float]] = {}
    for t in truths:
        region = t["region"]
        truth_utc = parse_iso(t["actual_depart_utc"])
        # Nearest exit event for this region.
        best = None
        best_gap = None
        for e in conn.execute(
            "SELECT event_utc FROM events WHERE region = ? AND transition = 'exit'",
            (region,),
        ).fetchall():
            ev = parse_iso(e["event_utc"])
            gap = abs((ev - truth_utc).total_seconds())
            if best_gap is None or gap < best_gap:
                best_gap = gap
                best = ev
        if best is not None and best_gap is not None and best_gap <= _MATCH_WINDOW_SEC:
            # Positive => recorded exit later than truth => iOS fired late.
            by_region.setdefault(region, []).append((best - truth_utc).total_seconds())

    out: list[RegionEstimate] = []
    for region in config.regions:
        samples = sorted(by_region.get(region, []))
        if samples:
            n = len(samples)
            mid = n // 2
            med = samples[mid] if n % 2 else (samples[mid - 1] + samples[mid]) / 2.0
            out.append(RegionEstimate(region, n, med, samples))
        else:
            out.append(RegionEstimate(region, 0, None, []))
    return out


def format_estimates(estimates: list[RegionEstimate]) -> str:
    lines = ["Suggested calibration_offset_sec (positive => iOS fired late):", ""]
    for e in estimates:
        if e.median_offset_sec is None:
            lines.append(f"  {e.region:<18} n=0   (add ground truth with `calibrate add`)")
        else:
            lines.append(
                f"  {e.region:<18} n={e.n:<3} median={e.median_offset_sec:+.0f}s   "
                f"samples={[round(s) for s in e.samples_sec]}"
            )
    return "\n".join(lines)

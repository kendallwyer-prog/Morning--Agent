"""`stats`: median / 80th percentile / n per segment type.

Only ``complete`` segments count. Incomplete (broken pairs) and suspect
(overlapping-geofence flips) are excluded so they can't poison a median.
Any bucket with n < ``min_n`` is suppressed rather than shown as a noisy
number (default 10).
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass


@dataclass
class Bucket:
    key: str
    n: int
    median_sec: float | None
    p80_sec: float | None
    suppressed: bool  # True when n < min_n


def _percentile(sorted_vals: list[int], p: float) -> float:
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * (p / 100.0)
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return float(sorted_vals[int(k)])
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _median(sorted_vals: list[int]) -> float:
    n = len(sorted_vals)
    mid = n // 2
    if n % 2:
        return float(sorted_vals[mid])
    return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0


def compute_stats(conn: sqlite3.Connection, min_n: int = 10) -> list[Bucket]:
    rows = conn.execute(
        "SELECT segment_type, from_region, to_region, duration_sec "
        "FROM segments WHERE status = 'complete' AND duration_sec IS NOT NULL"
    ).fetchall()

    buckets: dict[str, list[int]] = {}
    for r in rows:
        if r["segment_type"] == "dwell":
            key = f"dwell:{r['from_region']}"
        else:
            key = f"{r['from_region']}->{r['to_region']}"
        buckets.setdefault(key, []).append(r["duration_sec"])

    out: list[Bucket] = []
    for key in sorted(buckets):
        vals = sorted(buckets[key])
        n = len(vals)
        if n < min_n:
            out.append(Bucket(key, n, None, None, True))
        else:
            out.append(Bucket(key, n, _median(vals), _percentile(vals, 80), False))
    return out


def _fmt(sec: float | None) -> str:
    if sec is None:
        return "-"
    m, s = divmod(int(round(sec)), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def format_stats(buckets: list[Bucket], min_n: int = 10) -> str:
    if not buckets:
        return "No complete segments yet."
    lines = [f"{'segment':<28} {'n':>4} {'median':>9} {'p80':>9}", "-" * 54]
    for b in buckets:
        if b.suppressed:
            lines.append(f"{b.key:<28} {b.n:>4}   (suppressed, n<{min_n})")
        else:
            lines.append(
                f"{b.key:<28} {b.n:>4} {_fmt(b.median_sec):>9} {_fmt(b.p80_sec):>9}"
            )
    return "\n".join(lines)

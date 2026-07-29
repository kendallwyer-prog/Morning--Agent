"""`doctor --setup`: did you actually finish wiring up the phone?

Setup is ten hand-built iOS automations — an Arrive *and* a Leave for each of
five regions — each with a URL, a header, and four JSON fields. The failure
that matters isn't the one that errors; it's the one that half-works:

* **You built Arrive but not Leave for a region.** Enters keep landing, so
  ``doctor`` looks healthy and the region shows a recent timestamp. But a
  transit needs an *exit* to start it, so that leg silently produces no travel
  times, forever. Your alerts quietly run on ``fallback_travel_sec`` and the
  README's promise that estimates adapt never comes true for that place.
* **A region is configured but was never set up at all**, usually because the
  list in ``geofence.toml`` grew after you built the automations.
* **A commitment's leg has never completed once**, so the thing you actually
  care about — "how long does dorm → pool take me" — has no data behind it
  even though both endpoints look fine individually.

None of these raise. All of them are invisible in a "last event: 4 minutes ago"
health check. So they get their own check, run it on deploy day and after any
config change:

    python -m geofence doctor --setup
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .config import Config


@dataclass
class RegionCoverage:
    region: str
    enters: int
    exits: int

    @property
    def dead(self) -> bool:
        return self.enters == 0 and self.exits == 0

    @property
    def one_sided(self) -> bool:
        return not self.dead and (self.enters == 0 or self.exits == 0)


@dataclass
class LegCoverage:
    origin: str
    destination: str
    label: str
    complete: int


@dataclass
class SetupReport:
    ok: bool
    regions: list[RegionCoverage] = field(default_factory=list)
    legs: list[LegCoverage] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _legs(config: Config) -> list[tuple[str, str, str]]:
    """Every (origin, destination, label) the agent depends on, deduped."""
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for c in config.commitments:
        key = (c.origin, c.destination)
        if key not in seen:
            seen.add(key)
            out.append((c.origin, c.destination, c.label))
    return out


def run_setup_check(conn: sqlite3.Connection, config: Config) -> SetupReport:
    rep = SetupReport(ok=True)

    counts: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT region, transition, COUNT(*) c FROM events GROUP BY region, transition"
    ):
        counts.setdefault(row["region"], {})[row["transition"]] = row["c"]

    # Regions that are actually load-bearing: used by a commitment or by coffee.
    used: set[str] = {config.coffee.region}
    for c in config.commitments:
        used.update((c.origin, c.destination))

    for rid in config.regions:
        got = counts.get(rid, {})
        cov = RegionCoverage(rid, got.get("enter", 0), got.get("exit", 0))
        rep.regions.append(cov)
        name = config.region_name(rid)

        if cov.dead:
            msg = f"{name}: no events at all — automations never built, or never triggered."
            (rep.problems if rid in used else rep.notes).append(msg)
        elif cov.one_sided:
            missing = "Leave" if cov.exits == 0 else "Arrive"
            msg = (
                f"{name}: {cov.enters} arrivals, {cov.exits} departures — the "
                f"'{missing}' automation is missing. Travel times from here will "
                f"never be measured."
            )
            (rep.problems if rid in used else rep.notes).append(msg)

    for origin, dest, label in _legs(config):
        n = conn.execute(
            "SELECT COUNT(*) c FROM segments WHERE segment_type = 'transit' "
            "AND status = 'complete' AND from_region = ? AND to_region = ?",
            (origin, dest),
        ).fetchone()["c"]
        rep.legs.append(LegCoverage(origin, dest, label, n))
        if n == 0:
            rep.problems.append(
                f"{label} ({config.region_name(origin)} → "
                f"{config.region_name(dest)}): no completed trips recorded — "
                f"departure times are running on the configured guess."
            )
        elif n < config.alerting.min_samples:
            rep.notes.append(
                f"{label}: {n} of {config.alerting.min_samples} trips needed "
                f"before estimates stop using fallback_travel_sec."
            )

    rep.ok = not rep.problems
    return rep


def format_setup(rep: SetupReport) -> str:
    lines = [f"setup: {'OK' if rep.ok else 'INCOMPLETE'}", "", "  region coverage:"]
    lines.append(f"    {'region':<20} {'arrivals':>9} {'departures':>11}")
    for c in sorted(rep.regions, key=lambda r: r.region):
        flag = "  !" if (c.dead or c.one_sided) else ""
        lines.append(f"    {c.region:<20} {c.enters:>9} {c.exits:>11}{flag}")

    if rep.legs:
        lines.append("")
        lines.append("  commitment legs:")
        for leg in rep.legs:
            lines.append(
                f"    {leg.origin} → {leg.destination:<18} "
                f"{leg.complete:>4} complete trips"
            )

    if rep.problems:
        lines.append("")
        lines.append("  problems:")
        lines.extend(f"    ! {p}" for p in rep.problems)
    if rep.notes:
        lines.append("")
        lines.append("  notes:")
        lines.extend(f"    - {n}" for n in rep.notes)
    if rep.ok and not rep.notes:
        lines.append("")
        lines.append("  Every region reports both directions. Nothing to fix.")
    return "\n".join(lines)

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
    # Human-readable name for Telegram messages ("DeNunzio Pool" reads better
    # than "denunzio"). Falls back to the id with underscores turned to spaces.
    name: str | None = None


_DAY_IDS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass
class Commitment:
    """A recurring place you have to be, by a certain wall-clock time.

    The departure maths is:
        must_leave = arrive_by - travel_estimate - safety_margin_sec
        ping_at    = must_leave - prep_sec
    """

    id: str
    label: str
    origin: str            # region you leave FROM
    destination: str       # region you must arrive AT
    arrive_by: str         # local wall clock "HH:MM"
    days: list[str]        # subset of _DAY_IDS
    prep_sec: int          # how much warning you want before you must walk out
    safety_margin_sec: int  # cushion added on top of the travel estimate
    # Used until there are `min_samples` observed transits for this leg. Without
    # it the very first week would have no estimate at all.
    fallback_travel_sec: int

    def runs_on(self, weekday: int) -> bool:
        """weekday: Monday=0 .. Sunday=6, matching datetime.weekday()."""
        return _DAY_IDS[weekday] in self.days


@dataclass
class Alerting:
    # Travel estimates use this percentile, not the median: you care about
    # being on time, not about the typical case. p80 => late 1 trip in 5.
    percentile: float = 80.0
    # Below this many observed transits for a leg, fall back to the configured
    # fallback_travel_sec and SAY SO in the message rather than quoting a
    # confident-looking number derived from two data points.
    min_samples: int = 5
    # Only consider transits from the last N days -- your pace in December is
    # not your pace in September.
    lookback_days: int = 60
    # If the agent was down over a departure time, do NOT send the alert late;
    # a ping that arrives after you should already have left is worse than
    # silence. Skipped alerts are recorded with outcome 'missed_window'.
    max_late_send_sec: int = 300
    # After "on my way", if no exit from the origin is seen this long after the
    # planned departure, send one nudge. Never more than one.
    nudge_after_sec: int = 300
    # Grading: arriving within this many seconds of arrive_by counts as on time.
    on_time_grace_sec: int = 0
    # Advice: how many graded occurrences before suggesting a config change.
    advice_min_occurrences: int = 8
    # Advice: suggest leaving later if you are habitually this early.
    too_early_sec: int = 600
    # Health: once the data has gone silent and you've been told, don't tell you
    # again for this many hours. A warning you get every 20 seconds is one you
    # learn to ignore, which defeats the point of having it.
    silence_repeat_hours: float = 12.0


@dataclass
class Digest:
    """The weekly "here's how you're actually doing" message."""

    enabled: bool = True
    day: str = "sun"       # one of _DAY_IDS
    time: str = "19:00"    # local wall clock


@dataclass
class Coffee:
    region: str = "nassau_starbucks"
    # Time spent in the shop (queue + collect), added to the round trip.
    dwell_sec: int = 600
    # Never suggest a window that leaves less than this much slack before the
    # next commitment's must-leave time.
    slack_sec: int = 300
    # Fallbacks for legs to/from the coffee shop with no observed data yet.
    fallback_travel_sec: int = 600
    # Don't suggest coffee outside these local hours.
    earliest: str = "07:00"
    latest: str = "20:00"


@dataclass
class Telegram:
    # Never in the toml -- both come from the environment.
    bot_token: str | None = None
    chat_id: str | None = None
    # Long-poll timeout for getUpdates, and the scheduler tick.
    poll_timeout_sec: int = 25
    tick_sec: int = 20


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
    # --- Phase 2: alerting ---------------------------------------------------
    commitments: list[Commitment] = field(default_factory=list)
    alerting: Alerting = field(default_factory=Alerting)
    coffee: Coffee = field(default_factory=Coffee)
    telegram: Telegram = field(default_factory=Telegram)
    digest: Digest = field(default_factory=Digest)

    def region_name(self, region_id: str) -> str:
        """Display name for messages, falling back to a readable form of the id
        so an un-named region reads as "henry hall", never "None"."""
        r = self.regions.get(region_id)
        if r is not None and r.name:
            return r.name
        return region_id.replace("_", " ")

    def commitment(self, cid: str) -> Commitment | None:
        for c in self.commitments:
            if c.id == cid:
                return c
        return None

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
            name=r.get("name"),
            lat=r.get("lat"),
            lon=r.get("lon"),
            radius_m=float(r.get("radius_m", 100)),
            calibration_offset_sec=int(r.get("calibration_offset_sec", 0)),
            aliases=list(r.get("aliases", [])),
        )

    commitments = _load_commitments(data, regions)
    al = data.get("alerting", {})
    alerting = Alerting(
        percentile=float(al.get("percentile", 80.0)),
        min_samples=int(al.get("min_samples", 5)),
        lookback_days=int(al.get("lookback_days", 60)),
        max_late_send_sec=int(al.get("max_late_send_sec", 300)),
        nudge_after_sec=int(al.get("nudge_after_sec", 300)),
        on_time_grace_sec=int(al.get("on_time_grace_sec", 0)),
        advice_min_occurrences=int(al.get("advice_min_occurrences", 8)),
        too_early_sec=int(al.get("too_early_sec", 600)),
        silence_repeat_hours=float(al.get("silence_repeat_hours", 12.0)),
    )

    dg = data.get("digest", {})
    digest = Digest(
        enabled=bool(dg.get("enabled", True)),
        day=str(dg.get("day", "sun")).strip().lower()[:3],
        time=str(dg.get("time", "19:00")),
    )
    if digest.day not in _DAY_IDS:
        raise ValueError(f"[digest] day '{digest.day}' is not a valid day")
    _parse_hhmm(digest.time)

    cf = data.get("coffee", {})
    coffee = Coffee(
        region=cf.get("region", "nassau_starbucks"),
        dwell_sec=int(cf.get("dwell_sec", 600)),
        slack_sec=int(cf.get("slack_sec", 300)),
        fallback_travel_sec=int(cf.get("fallback_travel_sec", 600)),
        earliest=str(cf.get("earliest", "07:00")),
        latest=str(cf.get("latest", "20:00")),
    )
    if coffee.region not in regions:
        raise ValueError(f"[coffee] region '{coffee.region}' is not a defined region")
    _parse_hhmm(coffee.earliest)
    _parse_hhmm(coffee.latest)

    tg = data.get("telegram", {})
    telegram = Telegram(
        bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
        chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
        poll_timeout_sec=int(tg.get("poll_timeout_sec", 25)),
        tick_sec=int(tg.get("tick_sec", 20)),
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
        commitments=commitments,
        alerting=alerting,
        coffee=coffee,
        telegram=telegram,
        digest=digest,
    )


def _parse_hhmm(value: str) -> tuple[int, int]:
    """Validate + split a local wall-clock 'HH:MM'. Raises ValueError."""
    parts = str(value).split(":")
    if len(parts) != 2:
        raise ValueError(f"expected HH:MM, got {value!r}")
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(f"time out of range: {value!r}")
    return hh, mm


def _load_commitments(data: dict, regions: dict[str, Region]) -> list[Commitment]:
    """Parse + VALIDATE `[[commitments]]`.

    Validation is strict and happens at load time on purpose: a typo'd region
    id in a commitment would otherwise surface as a silently missing alert at
    5:30am, which is exactly the failure this project exists to prevent.
    """
    out: list[Commitment] = []
    seen: set[str] = set()
    for i, c in enumerate(data.get("commitments", [])):
        cid = c.get("id")
        if not cid:
            raise ValueError(f"[[commitments]] #{i + 1} is missing an `id`")
        if cid in seen:
            raise ValueError(f"duplicate commitment id '{cid}'")
        seen.add(cid)

        for key in ("origin", "destination"):
            rid = c.get(key)
            if rid not in regions:
                raise ValueError(
                    f"commitment '{cid}': {key} '{rid}' is not a defined region"
                )
        _parse_hhmm(c.get("arrive_by", ""))

        days = [str(d).strip().lower()[:3] for d in c.get("days", [])]
        bad = [d for d in days if d not in _DAY_IDS]
        if bad:
            raise ValueError(f"commitment '{cid}': unknown day(s) {bad}")
        if not days:
            raise ValueError(f"commitment '{cid}': `days` is empty")

        out.append(
            Commitment(
                id=cid,
                label=c.get("label", cid),
                origin=c["origin"],
                destination=c["destination"],
                arrive_by=c["arrive_by"],
                days=days,
                prep_sec=int(c.get("prep_sec", 600)),
                safety_margin_sec=int(c.get("safety_margin_sec", 300)),
                fallback_travel_sec=int(c.get("fallback_travel_sec", 600)),
            )
        )
    return out

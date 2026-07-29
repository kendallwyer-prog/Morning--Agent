""""Should I be leaving earlier or later?"

Worth being precise about what adapts on its own and what needs your say-so:

* The **travel estimate adapts automatically**. It is a rolling percentile over
  a ``lookback_days`` window, so as your real walking pace changes, every
  future departure time moves with it. No advice, no action, no config edit.
* This module tunes the two knobs that encode *preference*, not fact:
  ``safety_margin_sec`` (how much cushion you want beyond the estimate) and
  ``prep_sec`` (how long you actually take to get out the door after the ping).
  Those are yours to set, so they're surfaced as a concrete suggested edit
  rather than silently rewritten.

The target lateness rate falls out of the percentile you chose: at p80 the
estimate is expected to be beaten by roughly 1 trip in 5, and the safety margin
is what should absorb those. So being late materially more often than
``1 - percentile/100`` means the margin is too thin, and never using the margin
at all means it's too fat and you're standing around at the pool.

Skips, no-shows, and missed windows are excluded throughout — see
``grading`` for why each of those would otherwise corrupt the recommendation.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import alerts
from .config import Commitment, Config
from .grading import LATE, ON_TIME
from .ingest.timeutil import parse_iso
from .stats import _median

#: Extra tolerance on the expected lateness rate before nagging you about it.
#: Without it, a run of two unlucky mornings in a small sample would trip advice.
_LATE_RATE_TOLERANCE = 0.10

#: Don't suggest a change to prep_sec smaller than this — sub-two-minute
#: tinkering is noise, and acting on it would make the agent feel fussy.
_MIN_PREP_ADJUST_SEC = 120


@dataclass
class CommitmentAdvice:
    commitment: Commitment
    n: int
    late_rate: float | None
    median_slack_sec: float | None
    median_depart_delta_sec: float | None
    #: Human-readable verdict lines.
    lines: list[str] = field(default_factory=list)
    #: Concrete config edits, as (key, current, suggested).
    suggestions: list[tuple[str, int, int]] = field(default_factory=list)

    @property
    def enough_data(self) -> bool:
        return bool(self.lines) and self.n > 0


def _samples(conn: sqlite3.Connection, commitment_id: str, limit: int) -> list[dict]:
    """Recent graded occurrences that measure YOUR timing, newest first."""
    out: list[dict] = []
    for r in alerts.graded(conn, commitment_id, limit=limit):
        if r["outcome"] not in (ON_TIME, LATE):
            continue
        arrive_by = parse_iso(r["arrive_by_utc"])
        planned = parse_iso(r["planned_depart_utc"])
        arrival = parse_iso(r["actual_arrive_utc"]) if r["actual_arrive_utc"] else None
        depart = parse_iso(r["actual_depart_utc"]) if r["actual_depart_utc"] else None
        out.append(
            {
                "late": r["outcome"] == LATE,
                "slack": (arrive_by - arrival).total_seconds() if arrival else None,
                "depart_delta": (
                    (depart - planned).total_seconds() if depart else None
                ),
            }
        )
    return out


def advise_commitment(
    conn: sqlite3.Connection, config: Config, c: Commitment, limit: int = 40
) -> CommitmentAdvice:
    a = config.alerting
    rows = _samples(conn, c.id, limit)
    n = len(rows)

    if n < a.advice_min_occurrences:
        return CommitmentAdvice(
            commitment=c,
            n=n,
            late_rate=None,
            median_slack_sec=None,
            median_depart_delta_sec=None,
            lines=[
                f"Still learning — {n} of {a.advice_min_occurrences} trips needed "
                f"before I'd trust a recommendation."
            ],
        )

    late_rate = sum(1 for r in rows if r["late"]) / n
    slacks = sorted(int(r["slack"]) for r in rows if r["slack"] is not None)
    deltas = sorted(int(r["depart_delta"]) for r in rows if r["depart_delta"] is not None)
    med_slack = _median(slacks) if slacks else None
    med_delta = _median(deltas) if deltas else None

    adv = CommitmentAdvice(
        commitment=c,
        n=n,
        late_rate=late_rate,
        median_slack_sec=med_slack,
        median_depart_delta_sec=med_delta,
    )

    target_late_rate = 1.0 - (a.percentile / 100.0)

    # --- leave earlier / later -----------------------------------------------
    if late_rate > target_late_rate + _LATE_RATE_TOLERANCE:
        # Size the increase so the worst recent arrival would have been on time.
        worst_late = min(slacks) if slacks else 0          # most negative slack
        bump = max(60, int(abs(min(worst_late, 0))))
        adv.lines.append(
            f"Leave earlier: late {late_rate:.0%} of the last {n} trips "
            f"(expected ~{target_late_rate:.0%} at p{a.percentile:.0f})."
        )
        adv.suggestions.append(
            ("safety_margin_sec", c.safety_margin_sec, c.safety_margin_sec + bump)
        )
    elif med_slack is not None and med_slack > a.too_early_sec:
        # Trim the margin but keep too_early_sec of genuine cushion.
        trim = int(med_slack - a.too_early_sec)
        new_margin = max(0, c.safety_margin_sec - trim)
        if new_margin != c.safety_margin_sec:
            adv.lines.append(
                f"You could leave later: typically {med_slack / 60:.0f} min early, "
                f"and never late in the last {n} trips."
            )
            adv.suggestions.append(
                ("safety_margin_sec", c.safety_margin_sec, new_margin)
            )
    else:
        adv.lines.append(
            f"Timing looks right — late {late_rate:.0%} of {n} trips, "
            f"typically {(med_slack or 0) / 60:.0f} min to spare."
        )

    # --- prep time -----------------------------------------------------------
    # Independent of the above: this measures the gap between the ping and you
    # actually walking out, which is what prep_sec is supposed to cover.
    if med_delta is not None and med_delta > _MIN_PREP_ADJUST_SEC:
        adv.lines.append(
            f"You leave about {med_delta / 60:.0f} min after I say to — "
            f"I'll ping earlier if you raise prep_sec."
        )
        adv.suggestions.append(
            ("prep_sec", c.prep_sec, c.prep_sec + int(med_delta))
        )

    return adv


def advise_all(
    conn: sqlite3.Connection, config: Config, now: datetime | None = None
) -> list[CommitmentAdvice]:
    now = now or datetime.now(timezone.utc)
    return [advise_commitment(conn, config, c) for c in config.commitments]


def format_advice(items: list[CommitmentAdvice]) -> str:
    if not items:
        return "No commitments configured."
    out: list[str] = []
    for adv in items:
        out.append(f"{adv.commitment.label}:")
        for line in adv.lines:
            out.append(f"  {line}")
        for key, cur, new in adv.suggestions:
            out.append(f"  → geofence.toml: {key} {cur} → {new}")
    return "\n".join(out)

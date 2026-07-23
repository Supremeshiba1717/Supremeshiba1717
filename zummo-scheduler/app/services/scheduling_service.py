"""Deterministic schedule generation + fairness ranking. NO LLM here.

All the "scheduling logic" the brief wanted kept as plain code lives in this
module: who works which day, staffing min/max, and the fairness ranking that
both the initial draft and the "need more people" revision reuse.
"""

import json
import logging
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    AvailabilitySubmission,
    Schedule,
    Shift,
    StaffingRequirement,
    SubmissionStatus,
    WeeklyCycle,
)

log = logging.getLogger("scheduling")

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# --- Fairness ranking -------------------------------------------------------


def rank_candidates_for_day(
    db: Session, employee_ids: list[int], day: str
) -> list[int]:
    """Order employees fairest-first for a given weekday.

    Rule (from the brief's ask): prefer people who haven't worked this weekday
    in a while / not much. Concretely:
      1. Fewest times they've EVER worked this weekday (spread the load).
      2. Tie-break: longest since they last worked any shift at all
         (approximated by max shift id — higher id = more recent).

    Pure SQL over shift history; deterministic and cheap.
    """
    if not employee_ids:
        return []

    # Count how many times each employee has worked this weekday historically.
    day_counts = dict(
        db.query(Shift.employee_id, func.count(Shift.id))
        .filter(Shift.employee_id.in_(employee_ids), Shift.day == day)
        .group_by(Shift.employee_id)
        .all()
    )
    # Most recent shift id per employee (proxy for "worked recently").
    last_shift = dict(
        db.query(Shift.employee_id, func.max(Shift.id))
        .filter(Shift.employee_id.in_(employee_ids))
        .group_by(Shift.employee_id)
        .all()
    )

    def sort_key(eid: int):
        return (day_counts.get(eid, 0), last_shift.get(eid, 0))

    return sorted(employee_ids, key=sort_key)


# --- Staffing config --------------------------------------------------------


def _staffing(db: Session) -> dict[str, tuple[int, int]]:
    reqs = {r.day: (r.min_staff, r.max_staff) for r in
            db.query(StaffingRequirement).all()}
    # Sensible fallback if a day isn't configured.
    return {d: reqs.get(d, (1, 3)) for d in DAYS}


def _available_by_day(db: Session, cycle_id: int) -> dict[str, list[int]]:
    """Map each day -> list of employee_ids who said they can work it."""
    subs = (
        db.query(AvailabilitySubmission)
        .filter(
            AvailabilitySubmission.cycle_id == cycle_id,
            AvailabilitySubmission.status == SubmissionStatus.PARSED,
        )
        .all()
    )
    by_day: dict[str, list[int]] = {d: [] for d in DAYS}
    for s in subs:
        try:
            days = json.loads(s.parsed_days or "{}").get("days", [])
        except json.JSONDecodeError:
            days = []
        for d in days:
            if d in by_day:
                by_day[d].append(s.employee_id)
    return by_day


# --- Draft generation -------------------------------------------------------


def build_draft(db: Session, cycle: WeeklyCycle) -> Schedule:
    """Create the next schedule version for a cycle from parsed availability.

    For each day: take available employees, rank them fairly, and assign up to
    max_staff (at least min_staff if enough people are available). Increments
    the version so every revision keeps an auditable history.
    """
    staffing = _staffing(db)
    available = _available_by_day(db, cycle.id)

    prev_version = (
        db.query(func.max(Schedule.version))
        .filter(Schedule.cycle_id == cycle.id)
        .scalar()
    )
    schedule = Schedule(
        cycle_id=cycle.id,
        version=(prev_version or 0) + 1,
        generated_at=datetime.utcnow(),
    )
    db.add(schedule)
    db.flush()  # get schedule.id

    for day in DAYS:
        min_staff, max_staff = staffing[day]
        ranked = rank_candidates_for_day(db, available[day], day)
        chosen = ranked[:max_staff]
        for eid in chosen:
            db.add(Shift(schedule_id=schedule.id, employee_id=eid, day=day))
        if len(chosen) < min_staff:
            log.warning(
                "Day %s understaffed: %d assigned, min %d (not enough available)",
                day, len(chosen), min_staff,
            )

    db.commit()
    log.info("Built schedule v%d for cycle %d", schedule.version, cycle.id)
    return schedule


def latest_schedule(db: Session, cycle_id: int) -> Schedule | None:
    return (
        db.query(Schedule)
        .filter(Schedule.cycle_id == cycle_id)
        .order_by(Schedule.version.desc())
        .first()
    )


def clone_schedule_as_new_version(db: Session, base: Schedule) -> Schedule:
    """Copy a schedule's shifts into a new incremented version.

    Used by the revision loop: we build on the current draft rather than
    regenerating from scratch, so manually accepted offers persist.
    """
    new = Schedule(
        cycle_id=base.cycle_id,
        version=base.version + 1,
        generated_at=datetime.utcnow(),
    )
    db.add(new)
    db.flush()
    for sh in base.shifts:
        db.add(
            Shift(
                schedule_id=new.id,
                employee_id=sh.employee_id,
                day=sh.day,
                notes=sh.notes,
            )
        )
    db.commit()
    return new

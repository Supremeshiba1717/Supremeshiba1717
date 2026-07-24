"""Consent-based shift offers for the "need more people on <day>" revision.

Flow: rank fairest available candidates → text top one YES/NO → on accept,
add the shift; on decline/timeout, cascade to the next candidate; if the pool
runs dry, escalate back to Steve.

Only the yes/no reply parsing MIGHT touch the LLM, and only when the regex
can't decide — see routing in cycle_service.
"""

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AvailabilitySubmission,
    Employee,
    OfferStatus,
    Shift,
    ShiftOffer,
    SubmissionStatus,
    WeeklyCycle,
)
from app.services import scheduling_service, sms_service

log = logging.getLogger("offers")


def _available_unscheduled(db: Session, cycle: WeeklyCycle, day: str) -> list[int]:
    """Employees who listed `day` as available but aren't scheduled that day
    in the latest schedule, and don't already have a live/accepted offer."""
    schedule = scheduling_service.latest_schedule(db, cycle.id)
    already = set()
    if schedule:
        already = {
            sh.employee_id for sh in schedule.shifts if sh.day == day
        }

    # Employees who already have a pending/accepted offer for this day.
    offered = {
        o.employee_id
        for o in db.query(ShiftOffer).filter(
            ShiftOffer.cycle_id == cycle.id,
            ShiftOffer.day == day,
            ShiftOffer.status.in_([OfferStatus.PENDING, OfferStatus.ACCEPTED]),
        )
    }
    # Employees who already declined/expired for this day — don't re-ask.
    exhausted = {
        o.employee_id
        for o in db.query(ShiftOffer).filter(
            ShiftOffer.cycle_id == cycle.id,
            ShiftOffer.day == day,
            ShiftOffer.status.in_([OfferStatus.DECLINED, OfferStatus.EXPIRED]),
        )
    }

    candidates = []
    subs = (
        db.query(AvailabilitySubmission)
        .filter(
            AvailabilitySubmission.cycle_id == cycle.id,
            AvailabilitySubmission.status == SubmissionStatus.PARSED,
        )
        .all()
    )
    for s in subs:
        if s.employee_id in already | offered | exhausted:
            continue
        try:
            days = json.loads(s.parsed_days or "{}").get("days", [])
        except json.JSONDecodeError:
            days = []
        if day in days:
            candidates.append(s.employee_id)
    return candidates


def open_offers_for_day(
    db: Session, cycle: WeeklyCycle, day: str, count: int
) -> int:
    """Open up to `count` new offers to the fairest-ranked candidates.

    Returns the number of offers actually opened (may be < count if the pool
    is small). Escalates to Steve if nobody is available.
    """
    candidates = _available_unscheduled(db, cycle, day)
    ranked = scheduling_service.rank_candidates_for_day(db, candidates, day)

    # Respect the per-day attempt cap.
    prior_attempts = (
        db.query(ShiftOffer)
        .filter(ShiftOffer.cycle_id == cycle.id, ShiftOffer.day == day)
        .count()
    )
    room = max(0, settings.max_offer_attempts - prior_attempts)
    to_open = ranked[: min(count, room)]

    if not to_open:
        _escalate_unfillable(db, cycle, day)
        return 0

    for rank, eid in enumerate(to_open):
        emp = db.get(Employee, eid)
        offer = ShiftOffer(
            cycle_id=cycle.id,
            employee_id=eid,
            day=day,
            status=OfferStatus.PENDING,
            rank=prior_attempts + rank,
        )
        db.add(offer)
        body = (
            f"Hi {emp.name}! {settings.manager_name} needs an extra person on "
            f"{day} for the week of {cycle.week_start_date}. Can you work it? "
            f"Reply YES or NO."
        )
        sms_service.send_sms(
            db, emp.phone_number, body, employee_id=eid, cycle_id=cycle.id
        )
    db.commit()
    log.info("Opened %d offer(s) for %s in cycle %d", len(to_open), day, cycle.id)
    return len(to_open)


def handle_offer_response(
    db: Session, offer: ShiftOffer, accepted: bool
) -> None:
    """Apply an employee's YES/NO to a pending offer, cascading on decline."""
    offer.responded_at = datetime.utcnow()
    cycle = offer.cycle
    emp = db.get(Employee, offer.employee_id)

    if accepted:
        offer.status = OfferStatus.ACCEPTED
        # Add the shift to the latest schedule as a new version.
        base = scheduling_service.latest_schedule(db, cycle.id)
        new_schedule = scheduling_service.clone_schedule_as_new_version(db, base)
        db.add(
            Shift(
                schedule_id=new_schedule.id,
                employee_id=offer.employee_id,
                day=offer.day,
                notes="added via revision",
            )
        )
        db.commit()
        sms_service.send_sms(
            db, emp.phone_number,
            f"Thanks {emp.name}! You're on for {offer.day}.",
            employee_id=emp.id, cycle_id=cycle.id,
        )
        _notify_manager_fill(db, cycle, offer.day, emp.name, filled=True)
    else:
        offer.status = OfferStatus.DECLINED
        db.commit()
        sms_service.send_sms(
            db, emp.phone_number,
            f"No problem {emp.name}, thanks for letting us know.",
            employee_id=emp.id, cycle_id=cycle.id,
        )
        # Cascade: try the next fairest candidate for that day.
        opened = open_offers_for_day(db, cycle, offer.day, count=1)
        if opened == 0:
            log.info("Cascade for %s exhausted the candidate pool", offer.day)


def expire_stale_offers(db: Session) -> int:
    """Expire offers past the timeout and cascade to the next candidate.

    Called periodically by the scheduler. Returns number expired.
    """
    cutoff = datetime.utcnow() - timedelta(hours=settings.offer_timeout_hours)
    stale = (
        db.query(ShiftOffer)
        .filter(
            ShiftOffer.status == OfferStatus.PENDING,
            ShiftOffer.sent_at <= cutoff,
        )
        .all()
    )
    for offer in stale:
        offer.status = OfferStatus.EXPIRED
        db.commit()
        log.info("Offer %d to employee %d for %s expired",
                 offer.id, offer.employee_id, offer.day)
        open_offers_for_day(db, offer.cycle, offer.day, count=1)
    return len(stale)


# --- Manager notifications --------------------------------------------------


def _notify_manager_fill(
    db: Session, cycle: WeeklyCycle, day: str, name: str, filled: bool
) -> None:
    if filled:
        body = (
            f"Update: {name} accepted the extra {day} shift. Reply APPROVED to "
            f"finalize, or ask for more changes."
        )
    else:
        body = f"Update: still working on filling {day}."
    sms_service.send_sms(
        db, settings.manager_phone_number, body, cycle_id=cycle.id
    )


def _escalate_unfillable(db: Session, cycle: WeeklyCycle, day: str) -> None:
    body = (
        f"Couldn't fill {day} — nobody available said yes (or the pool's "
        f"exhausted). Want me to ask people who didn't list {day}, or leave it "
        f"short? Reply with what you'd like."
    )
    sms_service.send_sms(
        db, settings.manager_phone_number, body, cycle_id=cycle.id
    )
    log.warning("Escalated unfillable day %s to manager for cycle %d",
                day, cycle.id)

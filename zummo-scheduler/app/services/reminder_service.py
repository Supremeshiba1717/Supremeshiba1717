"""Reminder + non-responder handling.

Two entry points:
  - send_reminders(): nudge anyone who hasn't replied, respecting a cooldown.
  - mark_nonresponders(): after the hard cutoff, flag silent employees
    no_response so they're excluded from auto-assignment but stay visible.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AvailabilitySubmission,
    Employee,
    SmsDirection,
    SmsLog,
    SubmissionStatus,
    WeeklyCycle,
)
from app.services import sms_service

log = logging.getLogger("reminder")


def _non_responders(db: Session, cycle: WeeklyCycle) -> list[Employee]:
    """Active employees with no actual reply yet for this cycle."""
    subs = (
        db.query(AvailabilitySubmission)
        .filter(AvailabilitySubmission.cycle_id == cycle.id)
        .all()
    )
    replied_ids = {s.employee_id for s in subs if s.raw_text}
    return (
        db.query(Employee)
        .filter(Employee.active.is_(True), Employee.id.notin_(replied_ids or [0]))
        .all()
    )


def _recently_nudged(db: Session, employee_id: int, cycle_id: int) -> bool:
    """True if we texted this employee within the cooldown window."""
    cutoff = datetime.utcnow() - timedelta(
        hours=settings.nonresponder_cooldown_hours
    )
    recent = (
        db.query(SmsLog)
        .filter(
            SmsLog.employee_id == employee_id,
            SmsLog.cycle_id == cycle_id,
            SmsLog.direction == SmsDirection.OUTBOUND,
            SmsLog.created_at >= cutoff,
        )
        .first()
    )
    return recent is not None


def send_reminders(db: Session, cycle: WeeklyCycle) -> int:
    """Nudge non-responders (cooldown-limited). Returns count actually texted."""
    sent = 0
    for emp in _non_responders(db, cycle):
        if _recently_nudged(db, emp.id, cycle.id):
            continue
        body = (
            f"Hi {emp.name}, quick reminder from Zummo Bike — we still need your "
            f"availability for the week of {cycle.week_start_date}. Reply with "
            f"the days you can work whenever you get a sec!"
        )
        sms_service.send_sms(
            db, emp.phone_number, body, employee_id=emp.id, cycle_id=cycle.id
        )
        sent += 1
    log.info("Sent %d reminders for cycle %d", sent, cycle.id)
    return sent


def mark_nonresponders(db: Session, cycle: WeeklyCycle) -> int:
    """After hard cutoff: mark silent employees no_response. Returns count."""
    count = 0
    for emp in _non_responders(db, cycle):
        sub = (
            db.query(AvailabilitySubmission)
            .filter_by(employee_id=emp.id, cycle_id=cycle.id)
            .first()
        )
        if not sub:
            sub = AvailabilitySubmission(employee_id=emp.id, cycle_id=cycle.id)
            db.add(sub)
        sub.status = SubmissionStatus.NO_RESPONSE
        count += 1
    db.commit()
    log.info("Marked %d employees no_response for cycle %d", count, cycle.id)
    return count

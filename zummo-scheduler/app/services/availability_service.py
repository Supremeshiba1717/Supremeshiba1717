"""Sending availability requests, recording replies, and batch-parsing them."""

import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import (
    AvailabilitySubmission,
    Employee,
    SubmissionStatus,
    WeeklyCycle,
)
from app.services import llm_parser, sms_service

log = logging.getLogger("availability")


def request_availability(db: Session, cycle: WeeklyCycle) -> None:
    """Text every active employee asking for their availability."""
    employees = db.query(Employee).filter(Employee.active.is_(True)).all()
    for emp in employees:
        body = (
            f"Hi {emp.name}! It's the Zummo Bike scheduler. What days can you "
            f"work the week of {cycle.week_start_date}? Reply with the days "
            f"(e.g. 'Mon Wed Fri')."
        )
        sms_service.send_sms(
            db, emp.phone_number, body, employee_id=emp.id, cycle_id=cycle.id
        )
        # Pre-create an empty submission row so we can track non-responders.
        existing = (
            db.query(AvailabilitySubmission)
            .filter_by(employee_id=emp.id, cycle_id=cycle.id)
            .first()
        )
        if not existing:
            db.add(
                AvailabilitySubmission(
                    employee_id=emp.id,
                    cycle_id=cycle.id,
                    status=SubmissionStatus.UNPARSED,
                )
            )
    db.commit()
    log.info("Requested availability from %d employees", len(employees))


def record_reply(db: Session, employee: Employee, cycle: WeeklyCycle, text: str) -> None:
    """Store an inbound availability text (parsing happens later, batched)."""
    sub = (
        db.query(AvailabilitySubmission)
        .filter_by(employee_id=employee.id, cycle_id=cycle.id)
        .first()
    )
    if not sub:
        sub = AvailabilitySubmission(employee_id=employee.id, cycle_id=cycle.id)
        db.add(sub)
    sub.raw_text = text
    sub.submitted_at = datetime.utcnow()
    sub.status = SubmissionStatus.UNPARSED  # re-parse if they re-text
    db.commit()
    log.info("Recorded availability reply from %s", employee.name)


def parse_pending(db: Session, cycle: WeeklyCycle) -> None:
    """Batch-parse all unparsed replies for a cycle in ONE LLM call.

    Employees the model can't confidently parse are marked needs_review (raw
    text preserved) rather than dropped — Steve can eyeball them.
    """
    pending = (
        db.query(AvailabilitySubmission)
        .filter(
            AvailabilitySubmission.cycle_id == cycle.id,
            AvailabilitySubmission.status == SubmissionStatus.UNPARSED,
            AvailabilitySubmission.raw_text.isnot(None),
        )
        .all()
    )
    if not pending:
        return

    emp_names = {e.id: e.name for e in db.query(Employee).all()}
    replies = [
        {
            "employee_id": s.employee_id,
            "name": emp_names.get(s.employee_id, "?"),
            "text": s.raw_text,
        }
        for s in pending
    ]

    results = llm_parser.parse_availability_batch(replies)

    for sub in pending:
        parsed = results.get(sub.employee_id, {})
        if parsed.get("confident"):
            sub.parsed_days = json.dumps(
                {"days": parsed["days"], "caveats": parsed.get("caveats", "")}
            )
            sub.status = SubmissionStatus.PARSED
        else:
            sub.status = SubmissionStatus.NEEDS_REVIEW
            log.warning(
                "Reply from employee %d flagged needs_review: %r",
                sub.employee_id, sub.raw_text,
            )
    db.commit()

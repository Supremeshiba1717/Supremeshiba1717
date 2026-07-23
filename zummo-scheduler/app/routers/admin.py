"""Admin/testing endpoints — manually drive the cycle without waiting a week.

These are unauthenticated for local dev simplicity. ⚠️ Add auth before exposing
this app publicly (see README).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    AvailabilitySubmission,
    Employee,
    Schedule,
    ShiftOffer,
    SmsLog,
    WeeklyCycle,
)
from app.services import cycle_service, reminder_service, scheduling_service

log = logging.getLogger("admin")
router = APIRouter(prefix="/admin")


@router.post("/trigger-cycle")
def trigger_cycle(db: Session = Depends(get_db)):
    """Start a new weekly cycle NOW (the same thing the cron job does)."""
    cycle = cycle_service.start_cycle(db)
    return {"cycle_id": cycle.id, "week_start_date": cycle.week_start_date,
            "status": cycle.status}


@router.post("/build-draft")
def build_draft(db: Session = Depends(get_db)):
    """Parse collected replies and send the draft to the manager."""
    cycle = cycle_service.active_cycle(db)
    if not cycle:
        raise HTTPException(404, "no active cycle")
    schedule = cycle_service.build_and_send_draft(db, cycle)
    return {"cycle_id": cycle.id, "schedule_version": schedule.version,
            "status": cycle.status}


@router.post("/simulate-inbound")
def simulate_inbound(from_number: str, body: str, db: Session = Depends(get_db)):
    """Pretend a text arrived (bypasses Twilio) — the fastest way to test flows."""
    outcome = cycle_service.route_inbound(db, from_number, body)
    return {"outcome": outcome}


@router.post("/send-reminders")
def send_reminders(db: Session = Depends(get_db)):
    cycle = cycle_service.active_cycle(db)
    if not cycle:
        raise HTTPException(404, "no active cycle")
    n = reminder_service.send_reminders(db, cycle)
    return {"reminders_sent": n}


@router.post("/mark-nonresponders")
def mark_nonresponders(db: Session = Depends(get_db)):
    cycle = cycle_service.active_cycle(db)
    if not cycle:
        raise HTTPException(404, "no active cycle")
    n = reminder_service.mark_nonresponders(db, cycle)
    return {"marked_no_response": n}


@router.get("/status")
def status(db: Session = Depends(get_db)):
    """Snapshot of the current cycle: submissions, offers, failed sends."""
    cycle = cycle_service.active_cycle(db)
    if not cycle:
        return {"active_cycle": None}
    subs = (
        db.query(AvailabilitySubmission)
        .filter(AvailabilitySubmission.cycle_id == cycle.id)
        .all()
    )
    offers = db.query(ShiftOffer).filter(ShiftOffer.cycle_id == cycle.id).all()
    failed = (
        db.query(SmsLog)
        .filter(SmsLog.cycle_id == cycle.id, SmsLog.status == "failed")
        .count()
    )
    schedule = scheduling_service.latest_schedule(db, cycle.id)
    emp_lookup = {e.id: e.name for e in db.query(Employee).all()}
    return {
        "cycle_id": cycle.id,
        "week_start_date": cycle.week_start_date,
        "status": cycle.status,
        "submissions": [
            {
                "employee": emp_lookup.get(s.employee_id),
                "status": s.status,
                "raw_text": s.raw_text,
            }
            for s in subs
        ],
        "offers": [
            {"employee": emp_lookup.get(o.employee_id), "day": o.day,
             "status": o.status}
            for o in offers
        ],
        "failed_sms": failed,
        "latest_schedule_version": schedule.version if schedule else None,
    }


@router.post("/generate-pdf")
def generate_pdf(db: Session = Depends(get_db)):
    """Generate (but don't send) a PDF of the latest schedule — for previewing."""
    cycle = cycle_service.active_cycle(db)
    if not cycle:
        raise HTTPException(404, "no active cycle")
    from app.services import pdf_service
    schedule = scheduling_service.latest_schedule(db, cycle.id)
    if not schedule:
        raise HTTPException(404, "no schedule yet")
    path = pdf_service.generate_schedule_pdf(db, schedule)
    return {"pdf_path": path}

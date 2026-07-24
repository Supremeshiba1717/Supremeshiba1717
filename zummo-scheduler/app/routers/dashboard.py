"""Web dashboard — a light visual layer over the same services the SMS flow uses.

This is the optional "manager can review via a web link" nice-to-have. It does
NOT introduce a second source of truth or any new scheduling logic: every
action calls the exact same service functions the SMS path does. Revisions from
the web use structured buttons (day + more/fewer + count), so the web path is
fully deterministic — no LLM call needed to interpret a click.

⚠️ Unauthenticated for local dev. Add auth before exposing publicly.
"""

import json
import os

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    AvailabilitySubmission,
    CycleStatus,
    Employee,
    ShiftOffer,
    SmsLog,
    WeeklyCycle,
)
from app.services import cycle_service, scheduling_service
from app.utils.phone import normalize_phone

router = APIRouter()

_TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
templates = Jinja2Templates(directory=_TEMPLATE_DIR)

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _draft_grid(db: Session, cycle: WeeklyCycle):
    """Return (employee_names_sorted, {name: set(days)}) for the latest schedule."""
    schedule = scheduling_service.latest_schedule(db, cycle.id) if cycle else None
    if not schedule:
        return None, {}, {}
    emp_lookup = {e.id: e.name for e in db.query(Employee).all()}
    grid: dict[str, set] = {}
    for sh in schedule.shifts:
        name = emp_lookup.get(sh.employee_id, f"#{sh.employee_id}")
        grid.setdefault(name, set()).add(sh.day)
    return schedule, grid, emp_lookup


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    cycle = cycle_service.active_cycle(db)
    emp_lookup = {e.id: e.name for e in db.query(Employee).all()}

    submissions = []
    offers = []
    schedule = None
    grid = {}
    failed_sms = 0
    if cycle:
        subs = (
            db.query(AvailabilitySubmission)
            .filter(AvailabilitySubmission.cycle_id == cycle.id)
            .all()
        )
        for s in subs:
            days = []
            if s.parsed_days:
                try:
                    days = json.loads(s.parsed_days).get("days", [])
                except json.JSONDecodeError:
                    days = []
            submissions.append(
                {
                    "employee": emp_lookup.get(s.employee_id, "?"),
                    "status": s.status,
                    "raw_text": s.raw_text or "",
                    "days": days,
                }
            )
        offers = [
            {
                "employee": emp_lookup.get(o.employee_id, "?"),
                "day": o.day,
                "status": o.status,
            }
            for o in db.query(ShiftOffer).filter(ShiftOffer.cycle_id == cycle.id)
        ]
        failed_sms = (
            db.query(SmsLog)
            .filter(SmsLog.cycle_id == cycle.id, SmsLog.status == "failed")
            .count()
        )
        schedule, grid, _ = _draft_grid(db, cycle)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "cycle": cycle,
            "days": DAYS,
            "submissions": submissions,
            "offers": offers,
            "schedule": schedule,
            "grid": {name: grid[name] for name in sorted(grid)},
            "failed_sms": failed_sms,
            "can_review": cycle
            and cycle.status
            in (CycleStatus.AWAITING_APPROVAL, CycleStatus.REVISING),
        },
    )


# --- Actions (all POST -> redirect back to dashboard) -----------------------


@router.post("/web/trigger-cycle")
def web_trigger(db: Session = Depends(get_db)):
    if cycle_service.active_cycle(db) is None:
        cycle_service.start_cycle(db)
    return RedirectResponse("/", status_code=303)


@router.post("/web/build-draft")
def web_build_draft(db: Session = Depends(get_db)):
    cycle = cycle_service.active_cycle(db)
    if cycle:
        cycle_service.build_and_send_draft(db, cycle)
    return RedirectResponse("/", status_code=303)


@router.post("/web/approve")
def web_approve(db: Session = Depends(get_db)):
    cycle = cycle_service.active_cycle(db)
    if not cycle:
        raise HTTPException(404, "no active cycle")
    cycle_service.finalize_cycle(db, cycle)
    return RedirectResponse("/", status_code=303)


@router.post("/web/revise")
def web_revise(
    day: str = Form(...),
    direction: str = Form(...),
    count: int = Form(1),
    db: Session = Depends(get_db),
):
    """Structured revision from the dashboard buttons — no LLM involved."""
    cycle = cycle_service.active_cycle(db)
    if not cycle:
        raise HTTPException(404, "no active cycle")
    cycle_service.apply_revision(
        db, cycle, [{"day": day, "direction": direction, "count": count}]
    )
    return RedirectResponse("/", status_code=303)


# --- Employees admin (useful while ConnecTeam is stubbed) -------------------


@router.get("/employees", response_class=HTMLResponse)
def employees_page(request: Request, db: Session = Depends(get_db)):
    employees = db.query(Employee).order_by(Employee.name).all()
    return templates.TemplateResponse(
        "employees.html", {"request": request, "employees": employees}
    )


@router.post("/web/employees/add")
def web_add_employee(
    name: str = Form(...),
    phone_number: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        norm = normalize_phone(phone_number)
    except ValueError:
        raise HTTPException(400, "invalid phone number")
    if not db.query(Employee).filter_by(phone_number=norm).first():
        db.add(Employee(name=name, phone_number=norm, active=True, source="local"))
        db.commit()
    return RedirectResponse("/employees", status_code=303)


@router.post("/web/employees/{employee_id}/toggle")
def web_toggle_employee(employee_id: int, db: Session = Depends(get_db)):
    emp = db.get(Employee, employee_id)
    if emp:
        emp.active = not emp.active
        db.commit()
    return RedirectResponse("/employees", status_code=303)

"""Cycle orchestration: the state machine + inbound message routing.

This is the "brain" that decides what a given inbound text means and moves a
weekly cycle through its states. It leans on the other services for the actual
work (SMS, parsing, scheduling, offers, PDF).
"""

import logging
import re
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.integrations.connecteam import get_connecteam
from app.models import (
    AvailabilitySubmission,
    CycleStatus,
    Employee,
    OfferStatus,
    Schedule,
    Shift,
    ShiftOffer,
    SubmissionStatus,
    WeeklyCycle,
)
from app.services import (
    availability_service,
    llm_parser,
    offer_service,
    pdf_service,
    reminder_service,
    scheduling_service,
    sms_service,
)
from app.utils.phone import normalize_phone

log = logging.getLogger("cycle")

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# --- Employee sync ----------------------------------------------------------


def sync_employees(db: Session) -> int:
    """Pull roster via the ConnecTeam adapter; upsert; deactivate missing.

    Inactive/sabbatical employees (active=False from the adapter) are stored
    but never texted or scheduled.
    """
    adapter = get_connecteam()
    external = adapter.sync_employees()
    seen_phones = set()
    for ext in external:
        try:
            phone = normalize_phone(ext.phone_number)
        except ValueError:
            log.warning("Skipping employee %s — bad phone %r", ext.name,
                        ext.phone_number)
            continue
        seen_phones.add(phone)
        emp = db.query(Employee).filter_by(phone_number=phone).first()
        if emp:
            emp.name = ext.name
            emp.active = ext.active
            emp.connecteam_id = ext.external_id
            emp.source = "connecteam"
        else:
            db.add(
                Employee(
                    name=ext.name,
                    phone_number=phone,
                    active=ext.active,
                    connecteam_id=ext.external_id,
                    source="connecteam",
                )
            )
    db.commit()
    return len(external)


# --- Cycle lifecycle --------------------------------------------------------


def _next_week_start() -> str:
    """ISO date of the upcoming Monday."""
    today = date.today()
    days_ahead = (0 - today.weekday()) % 7 or 7  # next Monday, not today
    return (today + timedelta(days=days_ahead)).isoformat()


def start_cycle(db: Session) -> WeeklyCycle:
    """Kick off a new weekly cycle: sync roster, create cycle, request avail."""
    sync_employees(db)
    cycle = WeeklyCycle(
        week_start_date=_next_week_start(), status=CycleStatus.COLLECTING
    )
    db.add(cycle)
    db.commit()
    availability_service.request_availability(db, cycle)
    log.info("Started cycle %d for week %s", cycle.id, cycle.week_start_date)
    return cycle


def active_cycle(db: Session) -> WeeklyCycle | None:
    """The one cycle currently in-flight (not sent), if any."""
    return (
        db.query(WeeklyCycle)
        .filter(WeeklyCycle.status != CycleStatus.SENT)
        .order_by(WeeklyCycle.id.desc())
        .first()
    )


def build_and_send_draft(db: Session, cycle: WeeklyCycle) -> Schedule:
    """Parse pending replies, build the draft, send it to Steve for review."""
    availability_service.parse_pending(db, cycle)
    schedule = scheduling_service.build_draft(db, cycle)
    cycle.status = CycleStatus.AWAITING_APPROVAL
    db.commit()
    _send_draft_to_manager(db, cycle, schedule)
    return schedule


def _render_schedule_text(db: Session, schedule: Schedule) -> str:
    """Human-readable draft for texting to Steve."""
    emp_lookup = {e.id: e.name for e in db.query(Employee).all()}
    by_day: dict[str, list[str]] = {d: [] for d in DAYS}
    for sh in schedule.shifts:
        by_day[sh.day].append(emp_lookup.get(sh.employee_id, f"#{sh.employee_id}"))
    lines = [f"Draft schedule (v{schedule.version}) week of "
             f"{schedule.cycle.week_start_date}:"]
    for d in DAYS:
        names = ", ".join(sorted(by_day[d])) or "(nobody)"
        lines.append(f"{d}: {names}")

    # Flag people needing manual review / no response.
    flagged = (
        db.query(Employee)
        .join(AvailabilitySubmission)
        .filter(
            AvailabilitySubmission.cycle_id == schedule.cycle_id,
            AvailabilitySubmission.status.in_(
                [SubmissionStatus.NEEDS_REVIEW, SubmissionStatus.NO_RESPONSE]
            ),
        )
        .all()
    )
    if flagged:
        lines.append("Needs attention: " + ", ".join(e.name for e in flagged))
    lines.append("Reply APPROVED or tell me what to change.")
    return "\n".join(lines)


def _send_draft_to_manager(db: Session, cycle: WeeklyCycle, schedule: Schedule):
    sms_service.send_sms(
        db,
        settings.manager_phone_number,
        _render_schedule_text(db, schedule),
        cycle_id=cycle.id,
    )


# --- Inbound routing --------------------------------------------------------

_YES = {"yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay", "👍"}
_NO = {"no", "n", "nope", "nah", "can't", "cant"}


def route_inbound(db: Session, from_number: str, body: str) -> str:
    """Decide what an inbound text means and act on it.

    Priority order (per design):
      1. Pending shift offer for this number? -> YES/NO handling.
      2. Sender is the manager during review/revise? -> manager reply.
      3. Active cycle collecting? -> availability reply.
    Returns a short human-readable description of what happened (for logs/tests).
    """
    try:
        norm = normalize_phone(from_number)
    except ValueError:
        return "ignored: unparseable number"

    employee = db.query(Employee).filter_by(phone_number=norm).first()
    is_manager = norm == _safe_norm(settings.manager_phone_number)

    # 1. Pending offer?
    if employee:
        pending = (
            db.query(ShiftOffer)
            .filter(
                ShiftOffer.employee_id == employee.id,
                ShiftOffer.status == OfferStatus.PENDING,
            )
            .order_by(ShiftOffer.id.desc())
            .first()
        )
        if pending:
            accepted = _interpret_yes_no(body)
            offer_service.handle_offer_response(db, pending, accepted)
            return f"offer {'accepted' if accepted else 'declined'} by {employee.name}"

    # 2. Manager reply during review/revise?
    cycle = active_cycle(db)
    if is_manager and cycle and cycle.status in (
        CycleStatus.AWAITING_APPROVAL,
        CycleStatus.REVISING,
    ):
        return _handle_manager_reply(db, cycle, body)

    # 3. Availability reply?
    if employee and cycle and cycle.status == CycleStatus.COLLECTING:
        availability_service.record_reply(db, employee, cycle, body)
        return f"availability recorded for {employee.name}"

    # Nothing matched — log-only (webhook already logged it as inbound).
    return "no matching context"


def _safe_norm(num: str) -> str:
    try:
        return normalize_phone(num)
    except ValueError:
        return ""


def _interpret_yes_no(text: str) -> bool:
    """Cheap regex first; only fall back to the LLM on ambiguous phrasing."""
    t = re.sub(r"[^\w\s👍]", "", text.strip().lower())
    first = t.split()[0] if t.split() else ""
    if first in _YES or t in _YES:
        return True
    if first in _NO or t in _NO:
        return False
    # Ambiguous ("maybe if it's morning") — ask the LLM to classify.
    result = llm_parser.parse_manager_reply(text)
    # Reuse the classifier loosely: treat clear approve as yes, else no.
    return result.get("action") == "approve"


def _handle_manager_reply(db: Session, cycle: WeeklyCycle, body: str) -> str:
    parsed = llm_parser.parse_manager_reply(body)
    action = parsed.get("action")

    if action == "approve":
        finalize_cycle(db, cycle)
        return "approved -> finalized"

    if action == "unclear":
        sms_service.send_sms(
            db, settings.manager_phone_number,
            "Sorry, I didn't catch that. Reply APPROVED, or tell me e.g. "
            "'add one more Thursday' or 'fewer people Sunday'.",
            cycle_id=cycle.id,
        )
        return "manager reply unclear -> asked to clarify"

    # action == "revise"
    changes = parsed.get("changes", [])
    result = apply_revision(db, cycle, changes, nudge_nonresponders=True)
    return "revise: " + result if result else "revise: nothing actionable"


def apply_revision(
    db: Session,
    cycle: WeeklyCycle,
    changes: list[dict],
    *,
    nudge_nonresponders: bool = True,
    resend_draft_to_manager: bool = True,
) -> str:
    """Apply structured schedule changes. Shared by the SMS + web review paths.

    `changes` is a list of {day, direction, count}. This is fully deterministic
    (no LLM) — the LLM only ever produces the `changes` list upstream; the web
    dashboard builds the same list from buttons, so both paths converge here.
    """
    cycle.status = CycleStatus.REVISING
    db.commit()

    if nudge_nonresponders:
        # Nudge any remaining non-responders (cooldown-limited).
        reminder_service.send_reminders(db, cycle)

    handled = []
    had_decrease = False
    for change in changes:
        day = change.get("day")
        if day not in DAYS:
            continue
        direction = change.get("direction", "increase")
        count = int(change.get("count") or settings.default_offer_count)
        if direction == "increase":
            offer_service.open_offers_for_day(db, cycle, day, count)
            handled.append(f"+{count} {day} (asking candidates)")
        else:
            _reduce_day(db, cycle, day, count)
            handled.append(f"-{count} {day}")
            had_decrease = True

    # Reductions apply immediately, so we can re-send the updated draft now.
    # Increases wait on employee YES/NO before an updated draft goes out.
    if had_decrease and resend_draft_to_manager:
        latest = scheduling_service.latest_schedule(db, cycle.id)
        _send_draft_to_manager(db, cycle, latest)

    return "; ".join(handled)


def _reduce_day(db: Session, cycle: WeeklyCycle, day: str, count: int) -> None:
    """Remove `count` people from a day, dropping the least-fair-to-keep first.

    We remove those who've worked this weekday MOST (inverse of the add rule).
    """
    base = scheduling_service.latest_schedule(db, cycle.id)
    new = scheduling_service.clone_schedule_as_new_version(db, base)
    day_shifts = [sh for sh in new.shifts if sh.day == day]
    # Rank present employees fairest-first; remove from the LEAST fair end.
    ids = [sh.employee_id for sh in day_shifts]
    ranked = scheduling_service.rank_candidates_for_day(db, ids, day)
    to_remove = set(ranked[len(ranked) - count:]) if count < len(ranked) else set(ranked)
    for sh in day_shifts:
        if sh.employee_id in to_remove:
            db.delete(sh)
    db.commit()


# --- Finalize ---------------------------------------------------------------


def finalize_cycle(db: Session, cycle: WeeklyCycle) -> str:
    """Approve -> finalize -> PDF -> text everyone -> push to ConnecTeam stub."""
    cycle.status = CycleStatus.FINALIZED
    # Expire any leftover pending offers from an abandoned revision.
    for o in db.query(ShiftOffer).filter(
        ShiftOffer.cycle_id == cycle.id, ShiftOffer.status == OfferStatus.PENDING
    ):
        o.status = OfferStatus.EXPIRED
    db.commit()

    schedule = scheduling_service.latest_schedule(db, cycle.id)
    pdf_path = pdf_service.generate_schedule_pdf(db, schedule)

    # Text the finalized schedule (readable text) to all active employees.
    text = _render_final_text(db, schedule)
    media_url = None
    if settings.public_base_url:
        # MMS media must be a publicly reachable URL (see /files route + README).
        import os
        media_url = (
            f"{settings.public_base_url.rstrip('/')}/files/"
            f"{os.path.basename(pdf_path)}"
        )
    for emp in db.query(Employee).filter(Employee.active.is_(True)):
        sms_service.send_sms(
            db, emp.phone_number, text, employee_id=emp.id,
            cycle_id=cycle.id, media_url=media_url,
        )

    # Push to ConnecTeam (stubbed).
    get_connecteam().push_final_schedule(_schedule_payload(db, schedule))

    cycle.status = CycleStatus.SENT
    db.commit()
    log.info("Finalized + sent cycle %d (PDF %s)", cycle.id, pdf_path)
    return pdf_path


def _render_final_text(db: Session, schedule: Schedule) -> str:
    emp_lookup = {e.id: e.name for e in db.query(Employee).all()}
    by_day: dict[str, list[str]] = {d: [] for d in DAYS}
    for sh in schedule.shifts:
        by_day[sh.day].append(emp_lookup.get(sh.employee_id, f"#{sh.employee_id}"))
    lines = [f"✅ Final Zummo Bike schedule, week of "
             f"{schedule.cycle.week_start_date}:"]
    for d in DAYS:
        names = ", ".join(sorted(by_day[d])) or "(nobody)"
        lines.append(f"{d}: {names}")
    return "\n".join(lines)


def _schedule_payload(db: Session, schedule: Schedule) -> dict:
    """Adapter-agnostic representation handed to ConnecTeam.push_final_schedule."""
    return {
        "week_start_date": schedule.cycle.week_start_date,
        "version": schedule.version,
        "shifts": [
            {"employee_id": sh.employee_id, "day": sh.day, "notes": sh.notes}
            for sh in schedule.shifts
        ],
    }

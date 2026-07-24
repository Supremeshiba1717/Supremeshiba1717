"""APScheduler jobs — the automated weekly cadence.

Jobs (all deterministic, no LLM directly):
  - weekly kickoff (configurable Tue night): start a new cycle.
  - periodic reminder sweep: nudge non-responders while collecting.
  - periodic cutoff check: mark no_response + auto-build the draft after cutoff.
  - periodic offer-timeout sweep: expire/cascade stale shift offers.

Timing knobs are all in config/env. Kept intentionally simple; you can tune
the sweep intervals here.
"""

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.db import session_scope
from app.models import CycleStatus
from app.services import cycle_service, offer_service, reminder_service

log = logging.getLogger("scheduler")

# APScheduler day_of_week uses 0=Mon..6=Sun via 'mon'..'sun' strings.
_DOW = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def weekly_kickoff_job():
    with session_scope() as db:
        # Only start a new cycle if none is in-flight.
        if cycle_service.active_cycle(db) is None:
            cycle_service.start_cycle(db)
        else:
            log.info("Kickoff skipped — a cycle is already active")


def reminder_sweep_job():
    """While collecting, nudge non-responders past the reminder offset."""
    with session_scope() as db:
        cycle = cycle_service.active_cycle(db)
        if not cycle or cycle.status != CycleStatus.COLLECTING:
            return
        elapsed = datetime.utcnow() - cycle.created_at
        if elapsed >= timedelta(hours=settings.reminder_offset_hours):
            reminder_service.send_reminders(db, cycle)


def cutoff_job():
    """After the hard cutoff, mark no_response and build the draft."""
    with session_scope() as db:
        cycle = cycle_service.active_cycle(db)
        if not cycle or cycle.status != CycleStatus.COLLECTING:
            return
        elapsed = datetime.utcnow() - cycle.created_at
        if elapsed >= timedelta(hours=settings.response_cutoff_hours):
            reminder_service.mark_nonresponders(db, cycle)
            cycle_service.build_and_send_draft(db, cycle)
            log.info("Cutoff reached — built draft for cycle %d", cycle.id)


def offer_timeout_job():
    with session_scope() as db:
        offer_service.expire_stale_offers(db)


def create_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=settings.timezone)

    scheduler.add_job(
        weekly_kickoff_job,
        CronTrigger(
            day_of_week=_DOW[settings.cycle_kickoff_day_of_week % 7],
            hour=settings.cycle_kickoff_hour,
            minute=settings.cycle_kickoff_minute,
        ),
        id="weekly_kickoff",
        replace_existing=True,
    )
    # Sweeps run hourly; the jobs themselves check whether it's time to act.
    scheduler.add_job(reminder_sweep_job, "interval", hours=1,
                      id="reminder_sweep", replace_existing=True)
    scheduler.add_job(cutoff_job, "interval", hours=1,
                      id="cutoff", replace_existing=True)
    scheduler.add_job(offer_timeout_job, "interval", minutes=15,
                      id="offer_timeout", replace_existing=True)
    return scheduler

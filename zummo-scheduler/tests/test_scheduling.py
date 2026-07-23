"""Tests for the deterministic scheduling + fairness logic (no LLM)."""

import json

from app.models import (
    AvailabilitySubmission,
    Employee,
    Schedule,
    Shift,
    SubmissionStatus,
    WeeklyCycle,
)
from app.services import scheduling_service


def _emp(db, name, phone):
    e = Employee(name=name, phone_number=phone, active=True)
    db.add(e)
    db.commit()
    return e


def _submit(db, emp, cycle, days):
    db.add(
        AvailabilitySubmission(
            employee_id=emp.id,
            cycle_id=cycle.id,
            raw_text=" ".join(days),
            parsed_days=json.dumps({"days": days, "caveats": ""}),
            status=SubmissionStatus.PARSED,
        )
    )
    db.commit()


def test_build_draft_assigns_available_employees(db):
    cycle = WeeklyCycle(week_start_date="2026-07-27")
    db.add(cycle)
    db.commit()

    a = _emp(db, "Alex", "+15551110001")
    b = _emp(db, "Sam", "+15551110002")
    _submit(db, a, cycle, ["Mon", "Wed"])
    _submit(db, b, cycle, ["Mon"])

    schedule = scheduling_service.build_draft(db, cycle)

    mon = [s for s in schedule.shifts if s.day == "Mon"]
    wed = [s for s in schedule.shifts if s.day == "Wed"]
    assert {s.employee_id for s in mon} == {a.id, b.id}
    assert {s.employee_id for s in wed} == {a.id}


def test_fairness_prefers_least_worked_weekday(db):
    cycle = WeeklyCycle(week_start_date="2026-07-27")
    db.add(cycle)
    db.commit()
    a = _emp(db, "Alex", "+15551110001")
    b = _emp(db, "Sam", "+15551110002")

    # Give Alex a history of Thursdays; Sam none.
    sched = Schedule(cycle_id=cycle.id, version=0)
    db.add(sched)
    db.commit()
    for _ in range(3):
        db.add(Shift(schedule_id=sched.id, employee_id=a.id, day="Thu"))
    db.commit()

    ranked = scheduling_service.rank_candidates_for_day(db, [a.id, b.id], "Thu")
    # Sam (0 Thursdays) should rank ahead of Alex (3 Thursdays).
    assert ranked[0] == b.id


def test_respects_max_staff(db):
    cycle = WeeklyCycle(week_start_date="2026-07-27")
    db.add(cycle)
    db.commit()
    # 5 people all available Saturday, but default max_staff is 3.
    emps = [_emp(db, f"E{i}", f"+1555111000{i}") for i in range(5)]
    for e in emps:
        _submit(db, e, cycle, ["Sat"])

    schedule = scheduling_service.build_draft(db, cycle)
    sat = [s for s in schedule.shifts if s.day == "Sat"]
    assert len(sat) == 3  # capped at max_staff

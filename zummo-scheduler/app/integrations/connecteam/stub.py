"""Stub ConnecTeam adapter — used until real API docs + credentials exist.

⚠️  Everything here is scaffolding. I (Claude Code) do NOT have live access to
ConnecTeam's real API, so nothing below calls a real endpoint or guesses at
one. `sync_employees()` reads from the local DB so you can develop the whole
flow end-to-end; `push_final_schedule()` just logs what it *would* push.

To go live: create `real.py` with `class RealConnecTeam(ConnecTeamAdapter)`,
implement the two methods against the actual API, and switch the factory in
`__init__.py`. The rest of the app does not change.
"""

import logging

from app.db import session_scope
from app.integrations.connecteam.base import ConnecTeamAdapter, ExternalEmployee
from app.models import Employee

log = logging.getLogger("connecteam.stub")


class StubConnecTeam(ConnecTeamAdapter):
    def sync_employees(self) -> list[ExternalEmployee]:
        """Local-only stand-in: returns employees already in our DB.

        In the Real adapter this would GET the ConnecTeam roster and map each
        record into ExternalEmployee, setting `active=False` for anyone
        inactive/on sabbatical.

        TODO(real): call ConnecTeam users/list endpoint; map fields:
          external_id  <- <their user id field>
          name         <- <their name field(s)>
          phone_number <- <their phone field>
          active       <- <their active/archived/status field>
        """
        with session_scope() as db:
            rows = db.query(Employee).all()
            result = [
                ExternalEmployee(
                    external_id=e.connecteam_id or f"local-{e.id}",
                    name=e.name,
                    phone_number=e.phone_number,
                    active=e.active,
                )
                for e in rows
            ]
        log.info("StubConnecTeam.sync_employees -> %d employees", len(result))
        return result

    def push_final_schedule(self, schedule_payload: dict) -> None:
        """No-op stub: logs the payload it would send.

        TODO(real): map `schedule_payload` to ConnecTeam's scheduling/time-clock
        API and POST it. Include updating shift records so the app reflects it.
        """
        shift_count = len(schedule_payload.get("shifts", []))
        log.info(
            "StubConnecTeam.push_final_schedule -> WOULD push %d shifts for "
            "week %s (no-op until real API wired in)",
            shift_count,
            schedule_payload.get("week_start_date"),
        )

"""Abstract interface for the ConnecTeam integration.

The core app only ever talks to this interface, never to a concrete
ConnecTeam client directly. That's what makes it swappable: today we use
`StubConnecTeam`; once real API docs + a key exist, implement a
`RealConnecTeam(ConnecTeamAdapter)` and the rest of the app is unchanged.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ExternalEmployee:
    """Normalized employee record as the core app expects it.

    Whatever ConnecTeam's real JSON looks like, the Real adapter's job is to
    map it into THIS shape. Keeping the boundary here means the DB/sync code
    never sees ConnecTeam-specific fields.
    """

    external_id: str
    name: str
    phone_number: str
    active: bool  # False for inactive / "sabbatical" — never texted/scheduled


class ConnecTeamAdapter(ABC):
    @abstractmethod
    def sync_employees(self) -> list[ExternalEmployee]:
        """Return the current roster. Implementations MUST filter/mark inactive
        staff via the `active` flag — the caller relies on it to exclude
        sabbatical employees from texts and scheduling."""
        raise NotImplementedError

    @abstractmethod
    def push_final_schedule(self, schedule_payload: dict) -> None:
        """Push finalized shifts into ConnecTeam (incl. time-clock/shift records).

        `schedule_payload` is our internal, adapter-agnostic representation;
        the Real adapter maps it to ConnecTeam's API shape.
        """
        raise NotImplementedError

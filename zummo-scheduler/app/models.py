"""SQLAlchemy ORM models — the persistent store.

Schema mirrors the data model in the project brief, with a few additions
called out in the design: `staffing_requirements`, `sms_log`, and
`shift_offers` (for the consent-based revision loop).

Status values are kept as plain strings (not DB enums) so SQLite migrations
stay painless and a hobbyist can eyeball the table contents easily.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# --- Status constants (documentation + avoids typos across the codebase) ----


class CycleStatus:
    COLLECTING = "collecting"
    DRAFT_BUILT = "draft_built"
    AWAITING_APPROVAL = "awaiting_approval"
    REVISING = "revising"
    FINALIZED = "finalized"
    SENT = "sent"


class SubmissionStatus:
    UNPARSED = "unparsed"        # raw text received, not yet run through LLM
    PARSED = "parsed"           # successfully parsed into structured days
    NEEDS_REVIEW = "needs_review"  # LLM couldn't confidently parse — flag for human
    NO_RESPONSE = "no_response"    # employee never replied by cutoff


class OfferStatus:
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    EXPIRED = "expired"


class SmsDirection:
    OUTBOUND = "outbound"
    INBOUND = "inbound"


# --- Tables -----------------------------------------------------------------


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Stored normalized to E.164 (e.g. +15551234567) for reliable matching.
    phone_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # "connecteam" once synced from there, "local" if seeded manually.
    source: Mapped[str] = mapped_column(String(30), default="local", nullable=False)
    connecteam_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    submissions: Mapped[list["AvailabilitySubmission"]] = relationship(
        back_populates="employee"
    )
    shifts: Mapped[list["Shift"]] = relationship(back_populates="employee")


class WeeklyCycle(Base):
    __tablename__ = "weekly_cycles"

    id: Mapped[int] = mapped_column(primary_key=True)
    week_start_date: Mapped[str] = mapped_column(String(10), nullable=False)  # ISO date
    status: Mapped[str] = mapped_column(
        String(30), default=CycleStatus.COLLECTING, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    submissions: Mapped[list["AvailabilitySubmission"]] = relationship(
        back_populates="cycle"
    )
    schedules: Mapped[list["Schedule"]] = relationship(back_populates="cycle")
    offers: Mapped[list["ShiftOffer"]] = relationship(back_populates="cycle")


class AvailabilitySubmission(Base):
    __tablename__ = "availability_submissions"
    # One (latest) submission per employee per cycle; re-texts update in place.
    __table_args__ = (UniqueConstraint("employee_id", "cycle_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    cycle_id: Mapped[int] = mapped_column(ForeignKey("weekly_cycles.id"))
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON string: {"days": ["Mon","Wed"], "caveats": "off Fri this week"}
    parsed_days: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default=SubmissionStatus.UNPARSED, nullable=False
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    employee: Mapped["Employee"] = relationship(back_populates="submissions")
    cycle: Mapped["WeeklyCycle"] = relationship(back_populates="submissions")


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("weekly_cycles.id"))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    cycle: Mapped["WeeklyCycle"] = relationship(back_populates="schedules")
    shifts: Mapped[list["Shift"]] = relationship(
        back_populates="schedule", cascade="all, delete-orphan"
    )


class Shift(Base):
    __tablename__ = "shifts"

    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("schedules.id"))
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    day: Mapped[str] = mapped_column(String(10), nullable=False)  # "Mon".."Sun"
    notes: Mapped[str | None] = mapped_column(String(200), nullable=True)

    schedule: Mapped["Schedule"] = relationship(back_populates="shifts")
    employee: Mapped["Employee"] = relationship(back_populates="shifts")


class StaffingRequirement(Base):
    """Editable per-day min/max staffing. Seeded from config YAML.

    ADDED beyond the brief's data model: the scheduling algorithm needs to
    know how many people each day wants. Real Zummo rules unknown — tune here.
    """

    __tablename__ = "staffing_requirements"

    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)
    min_staff: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_staff: Mapped[int] = mapped_column(Integer, default=3, nullable=False)


class ShiftOffer(Base):
    """A consent-based offer to fill an extra shift during the revision loop.

    ADDED beyond the brief: supports "need more people Thursday" → ask the
    fairest-ranked available employee YES/NO before assigning them.
    """

    __tablename__ = "shift_offers"

    id: Mapped[int] = mapped_column(primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("weekly_cycles.id"))
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"))
    day: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default=OfferStatus.PENDING, nullable=False
    )
    # Rank at time of offer, so cascades are auditable.
    rank: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    cycle: Mapped["WeeklyCycle"] = relationship(back_populates="offers")
    employee: Mapped["Employee"] = relationship()


class SmsLog(Base):
    """Every inbound/outbound message, for debugging delivery + unmatched texts."""

    __tablename__ = "sms_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id"), nullable=True
    )
    cycle_id: Mapped[int | None] = mapped_column(
        ForeignKey("weekly_cycles.id"), nullable=True
    )
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(20), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    twilio_sid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # "sent" / "failed" / "received" / "unmatched"
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

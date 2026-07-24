"""Shared pytest fixtures: an in-memory SQLite DB per test."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, StaffingRequirement

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    # Default staffing: min 1 / max 3 for every day.
    for d in DAYS:
        session.add(StaffingRequirement(day=d, min_staff=1, max_staff=3))
    session.commit()
    yield session
    session.close()

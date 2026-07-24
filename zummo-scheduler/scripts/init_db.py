"""Create all tables and seed staffing requirements from the YAML config.

Run once before starting the app:  python -m scripts.init_db

Uses SQLAlchemy's create_all (not Alembic) to keep first-run dead simple. If
you later change the schema, either drop zummo.db and re-run this, or add
Alembic migrations.
"""

import os
import sys

import yaml

# Allow running as `python scripts/init_db.py` too.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db import engine, session_scope  # noqa: E402
from app.models import Base, StaffingRequirement  # noqa: E402

CONFIG_PATH = os.path.join("config", "staffing_requirements.yaml")


def main():
    print("Creating tables...")
    Base.metadata.create_all(engine)

    print("Seeding staffing requirements from", CONFIG_PATH)
    with open(CONFIG_PATH) as f:
        reqs = yaml.safe_load(f)

    with session_scope() as db:
        for day, cfg in reqs.items():
            existing = db.query(StaffingRequirement).filter_by(day=day).first()
            if existing:
                existing.min_staff = cfg["min"]
                existing.max_staff = cfg["max"]
            else:
                db.add(
                    StaffingRequirement(
                        day=day, min_staff=cfg["min"], max_staff=cfg["max"]
                    )
                )
    print("Done. Database ready.")


if __name__ == "__main__":
    main()

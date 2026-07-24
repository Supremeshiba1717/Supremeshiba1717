"""Seed fake employees for local end-to-end testing (no ConnecTeam needed).

Run:  python -m scripts.seed_test_data

Includes one inactive/"sabbatical" employee to verify they're excluded from
texts and scheduling. Phone numbers here are placeholders — for real inbound
testing, change them to numbers you've verified in your Twilio trial account
(or your own phone), so replies actually route back.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db import session_scope  # noqa: E402
from app.models import Employee  # noqa: E402
from app.utils.phone import normalize_phone  # noqa: E402

# name, phone, active
TEST_EMPLOYEES = [
    ("Alex Rivera", "+15005550101", True),
    ("Sam Chen", "+15005550102", True),
    ("Jordan Blake", "+15005550103", True),
    ("Casey Nguyen", "+15005550104", True),
    ("Dana Flores", "+15005550105", True),
    ("Pat Sabbatical", "+15005550199", False),  # inactive — must be excluded
]


def main():
    with session_scope() as db:
        for name, phone, active in TEST_EMPLOYEES:
            norm = normalize_phone(phone)
            emp = db.query(Employee).filter_by(phone_number=norm).first()
            if emp:
                emp.name, emp.active = name, active
            else:
                db.add(
                    Employee(
                        name=name, phone_number=norm, active=active, source="local"
                    )
                )
    print(f"Seeded {len(TEST_EMPLOYEES)} test employees "
          f"(1 inactive, should be excluded).")


if __name__ == "__main__":
    main()

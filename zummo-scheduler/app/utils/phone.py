"""Phone-number normalization so inbound texts reliably match employees.

We store and compare everything in E.164-ish form (+<countrycode><number>).
Kept deliberately simple: US-centric, since Zummo is a single US shop.

ASSUMPTION: 10-digit numbers are US (+1). If Zummo ever has non-US staff,
this needs a real library like `phonenumbers`.
"""

import re


def normalize_phone(raw: str) -> str:
    """Return a best-effort E.164 string. Raises ValueError if unusable."""
    if not raw:
        raise ValueError("empty phone number")

    # Strip everything except digits and a leading +.
    cleaned = raw.strip()
    has_plus = cleaned.startswith("+")
    digits = re.sub(r"\D", "", cleaned)

    if not digits:
        raise ValueError(f"no digits in phone number: {raw!r}")

    if has_plus:
        return "+" + digits

    if len(digits) == 10:  # bare US number
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits

    # Fallback: assume already includes country code.
    return "+" + digits


def phones_match(a: str, b: str) -> bool:
    try:
        return normalize_phone(a) == normalize_phone(b)
    except ValueError:
        return False

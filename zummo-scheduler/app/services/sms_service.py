"""Twilio SMS wrapper — outbound sending with retry, backoff, and logging.

Every send is recorded in `sms_log` whether it succeeds or fails, so a failed
delivery is never silently dropped — it shows up in /admin/status.
"""

import logging
import time

from sqlalchemy.orm import Session
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from app.config import settings
from app.models import SmsDirection, SmsLog
from app.utils.phone import normalize_phone

log = logging.getLogger("sms")

_client: Client | None = None


def _get_client() -> Client:
    global _client
    if _client is None:
        _client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    return _client


def send_sms(
    db: Session,
    to_number: str,
    body: str,
    *,
    employee_id: int | None = None,
    cycle_id: int | None = None,
    media_url: str | None = None,
    max_retries: int = 3,
) -> SmsLog:
    """Send one SMS/MMS. Retries on transient Twilio errors with backoff.

    Returns the SmsLog row (status "sent" or "failed"). Never raises on a
    delivery failure — the caller inspects the returned row's status instead,
    so one bad number doesn't abort a whole batch send.
    """
    try:
        to_normalized = normalize_phone(to_number)
    except ValueError as e:
        log.error("Cannot send — bad number %r: %s", to_number, e)
        row = SmsLog(
            employee_id=employee_id,
            cycle_id=cycle_id,
            direction=SmsDirection.OUTBOUND,
            phone_number=to_number,
            body=body,
            status="failed",
        )
        db.add(row)
        db.commit()
        return row

    twilio_sid = None
    status = "failed"
    delay = 2.0

    for attempt in range(1, max_retries + 1):
        try:
            kwargs = {
                "to": to_normalized,
                "from_": settings.twilio_from_number,
                "body": body,
            }
            if media_url:
                kwargs["media_url"] = [media_url]
            msg = _get_client().messages.create(**kwargs)
            twilio_sid = msg.sid
            status = "sent"
            break
        except TwilioRestException as e:
            log.warning(
                "Twilio send attempt %d/%d to %s failed: %s",
                attempt,
                max_retries,
                to_normalized,
                e,
            )
            if attempt < max_retries:
                time.sleep(delay)
                delay *= 2  # exponential backoff: 2s, 4s, 8s
        except Exception as e:  # noqa: BLE001 — never let a send crash the caller
            log.exception("Unexpected Twilio error to %s: %s", to_normalized, e)
            break

    row = SmsLog(
        employee_id=employee_id,
        cycle_id=cycle_id,
        direction=SmsDirection.OUTBOUND,
        phone_number=to_normalized,
        body=body,
        twilio_sid=twilio_sid,
        status=status,
    )
    db.add(row)
    db.commit()

    if status == "failed":
        log.error("SMS to %s permanently failed after %d attempts", to_normalized,
                  max_retries)
    return row


def log_inbound(
    db: Session,
    from_number: str,
    body: str,
    *,
    twilio_sid: str | None = None,
    employee_id: int | None = None,
    cycle_id: int | None = None,
    status: str = "received",
) -> SmsLog:
    """Record an inbound message (matched or unmatched) for the audit trail."""
    row = SmsLog(
        employee_id=employee_id,
        cycle_id=cycle_id,
        direction=SmsDirection.INBOUND,
        phone_number=from_number,
        body=body,
        twilio_sid=twilio_sid,
        status=status,
    )
    db.add(row)
    db.commit()
    return row

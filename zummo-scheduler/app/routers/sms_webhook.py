"""Twilio inbound SMS webhook.

Twilio POSTs form-encoded data here whenever the shop number receives a text.
We verify the signature (unless in test mode), log the message, and hand it to
the cycle router to decide what it means.
"""

import logging

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session
from twilio.request_validator import RequestValidator

from app.config import settings
from app.db import get_db
from app.models import Employee
from app.services import cycle_service, sms_service
from app.utils.phone import normalize_phone

log = logging.getLogger("webhook")
router = APIRouter()


def _valid_signature(request: Request, form: dict, url: str) -> bool:
    if settings.twilio_test_mode:
        return True  # test creds don't produce valid signatures locally
    validator = RequestValidator(settings.twilio_auth_token)
    signature = request.headers.get("X-Twilio-Signature", "")
    return validator.validate(url, form, signature)


@router.post("/webhook/sms")
async def inbound_sms(request: Request, db: Session = Depends(get_db)):
    form = dict(await request.form())
    from_number = form.get("From", "")
    body = form.get("Body", "")
    twilio_sid = form.get("MessageSid")

    if not _valid_signature(request, form, str(request.url)):
        log.warning("Rejected inbound webhook: bad Twilio signature")
        return Response(status_code=403)

    # Match to an employee if possible (for the log's FK); unmatched is fine.
    employee = None
    try:
        norm = normalize_phone(from_number)
        employee = db.query(Employee).filter_by(phone_number=norm).first()
    except ValueError:
        pass

    sms_service.log_inbound(
        db, from_number, body,
        twilio_sid=twilio_sid,
        employee_id=employee.id if employee else None,
        status="received" if employee else "unmatched",
    )

    try:
        outcome = cycle_service.route_inbound(db, from_number, body)
        log.info("Inbound from %s handled: %s", from_number, outcome)
    except Exception:  # noqa: BLE001 — a bad message must not 500 the webhook
        log.exception("Error routing inbound message from %s", from_number)

    # Empty TwiML: we send any replies ourselves via the REST API.
    return Response(content="<Response></Response>", media_type="application/xml")

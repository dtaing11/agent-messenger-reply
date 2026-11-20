"""Twilio webhooks: owner text commands and SMS delivery status.

Both check X-Twilio-Signature against the public URL Twilio called (PUBLIC_BASE_URL + path),
because behind the tunnel the app itself only sees http://localhost.
"""

import logging
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Request, Response

from app.config import Settings
from app.deps import get_db, get_runtime_settings
from app.integrations import twilio
from app.services import owner_commands
from app.services.business import load_business
from app.settings.runtime import set_marker
from app.timeutil import utcnow

log = logging.getLogger(__name__)
router = APIRouter()


def twiml(message: str | None) -> Response:
    body = f"<Message>{escape(message)}</Message>" if message else ""
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>', media_type="application/xml")


async def _verified_form(request: Request, settings: Settings, path: str) -> list[tuple[str, str]] | None:
    form = await request.form()
    params = [(k, str(v)) for k, v in form.multi_items()]
    base = settings.public_base_url.rstrip("/")
    url = f"{base}{path}" + (f"?{request.url.query}" if request.url.query else "")
    if not twilio.valid_signature(settings.twilio_auth_token.get_secret_value(), url, params,
                                  request.headers.get("X-Twilio-Signature")):
        return None
    return params


@router.post("/webhooks/sms")
async def incoming_sms(request: Request, conn=Depends(get_db), settings: Settings = Depends(get_runtime_settings)):
    params = await _verified_form(request, settings, "/webhooks/sms")
    if params is None:
        set_marker(conn, "twilio.last_signature_failure", utcnow().isoformat())
        return Response(status_code=403)
    form = dict(params)
    sender = form.get("From", "")
    if not twilio.is_owner(settings, sender):
        log.info("ignored SMS from a number that isn't an owner phone")
        return twiml(None)
    set_marker(conn, "twilio.last_inbound", utcnow().isoformat())
    biz = load_business(settings.business_path)
    reply = owner_commands.handle(conn, settings, biz, sender, form.get("Body", ""), utcnow())
    return twiml(reply)


@router.post("/webhooks/sms/status")
async def sms_status(request: Request, conn=Depends(get_db), settings: Settings = Depends(get_runtime_settings)):
    params = await _verified_form(request, settings, "/webhooks/sms/status")
    if params is None:
        return Response(status_code=403)
    form = dict(params)
    status = form.get("MessageStatus", "")
    sid = form.get("MessageSid", "")
    if sid and status in ("delivered", "undelivered", "failed"):
        new = "delivered" if status == "delivered" else "failed"
        err = f"Twilio {status} (code {form.get('ErrorCode')})" if new == "failed" else None
        conn.execute("UPDATE notifications SET status = ?, last_error = COALESCE(?, last_error) WHERE provider_id = ?",
                     (new, err, sid))
    return Response(status_code=204)

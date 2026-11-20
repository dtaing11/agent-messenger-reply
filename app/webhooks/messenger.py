"""Meta Messenger webhook: verification, signature check, dedup and durable enqueue.

The handler does no business work. It verifies, persists and returns 200 quickly; the
message worker does the rest.
"""

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse

from app.config import Settings, get_settings
from app.deps import get_db
from app.workers import queue

log = logging.getLogger(__name__)
router = APIRouter()


def verify_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Validate X-Hub-Signature-256 (HMAC-SHA256 of the raw body) in constant time."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


@router.get("/webhooks/messenger", response_class=PlainTextResponse)
def verify_webhook(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
    settings: Settings = Depends(get_settings),
):
    expected = settings.meta_verify_token.get_secret_value()
    if mode == "subscribe" and expected and hmac.compare_digest(token, expected):
        return challenge
    raise HTTPException(status_code=403, detail="verification failed")


def classify(event: dict) -> tuple[str, str] | None:
    """Return (event_type, external_id) for an entry.messaging item, or None to drop it."""
    if "message" in event:
        msg = event["message"]
        mid = msg.get("mid")
        if not mid:
            return None
        return ("echo" if msg.get("is_echo") else "message", mid)
    if "postback" in event:
        pb = event["postback"]
        ext = pb.get("mid") or f"postback:{event.get('sender', {}).get('id')}:{event.get('timestamp')}:{pb.get('payload')}"
        return ("postback", ext)
    return None  # delivery/read receipts and other fields are not needed yet


@router.post("/webhooks/messenger")
async def receive_webhook(request: Request, settings: Settings = Depends(get_settings), conn=Depends(get_db)):
    body = await request.body()
    if not verify_signature(settings.meta_app_secret.get_secret_value(), body, request.headers.get("x-hub-signature-256")):
        raise HTTPException(status_code=403, detail="invalid signature")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json")
    if data.get("object") != "page":
        return Response(status_code=404)

    accepted = duplicates = 0
    for entry in data.get("entry", []):
        page_id = str(entry.get("id", ""))
        if settings.meta_page_id and page_id != settings.meta_page_id:
            log.warning("ignoring event for unexpected page")
            continue
        for event in entry.get("messaging", []):
            kind = classify(event)
            if kind is None:
                continue
            event_type, ext_id = kind
            # For echoes the customer is the recipient; otherwise the sender.
            psid = (event.get("recipient") if event_type == "echo" else event.get("sender", {})).get("id")
            payload = {"page_id": page_id, "psid": psid, "event": event}
            if queue.enqueue(conn, "messenger", f"{page_id}:{ext_id}", event_type, payload, customer_key=f"{page_id}:{psid}"):
                accepted += 1
            else:
                duplicates += 1
    log.info("messenger webhook accepted=%d duplicates=%d", accepted, duplicates)
    return PlainTextResponse("EVENT_RECEIVED")

"""Sends queued owner alerts (the notifications table): texts through Twilio, and Messenger
messages to linked owner accounts, with retries.

A row is leased while sending (status 'sending', next_attempt_at = lease expiry) so a crash
mid-send is retried later instead of being lost. A Messenger alert for an owner whose 24-hour
window has closed is held (status 'failed', last_error 'window_closed: ...') and queued again
when that owner next writes to the Page (see owner_accounts.touch).
"""

import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import httpx

from app.config import Settings
from app.integrations import twilio
from app.integrations.messenger import OWNER_CMD, MessengerClient
from app.services import owner_accounts
from app.timeutil import to_db

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
LEASE = timedelta(minutes=5)
WINDOW_CLOSED = "window_closed: this owner hasn't messaged the Page in 24 hours; it's sent when they next do"

Sender = Callable[[Settings, str, str, str | None], Awaitable[str]]
FbSender = Callable[[Settings, str, str, list[list[str]] | None], Awaitable[str]]


async def _default_sender(settings: Settings, to: str, body: str, status_callback: str | None) -> str:
    return await twilio.send_sms(settings, to, body, status_callback)


class MessengerSendError(Exception):
    def __init__(self, message: str, retryable: bool = True, window_closed: bool = False):
        super().__init__(message)
        self.retryable = retryable
        self.window_closed = window_closed


async def _default_fb_sender(settings: Settings, psid: str, body: str, buttons: list[list[str]] | None) -> str:
    """Owner alert on Messenger. UPDATE: a message the owner didn't just ask for, inside their window."""
    client = MessengerClient(settings)
    try:
        if buttons:
            res = await client.send_choices(psid, body, [t for t, _ in buttons], messaging_type="UPDATE",
                                            payloads=[OWNER_CMD + c for _, c in buttons])
        else:
            res = await client.send_text(psid, body, messaging_type="UPDATE")
    except httpx.HTTPStatusError as exc:
        try:
            err = exc.response.json().get("error", {})
        except ValueError:
            err = {}
        code, sub = err.get("code"), err.get("error_subcode")
        # 10/2018278: outside the allowed window. 551 or 10 alone: the person can't get messages from
        # this app (in Development mode they need a role on the app); shown in the portal as failed.
        if sub == 2018278:
            raise MessengerSendError(WINDOW_CLOSED, retryable=False, window_closed=True) from exc
        msg = f"Messenger error {code}: {err.get('message', '')[:200]}"
        raise MessengerSendError(msg, retryable=exc.response.status_code >= 500 or code in (1, 2, 4, 613)) from exc
    return str(res.get("message_id", ""))


def _status_callback(settings: Settings) -> str | None:
    base = settings.public_base_url.rstrip("/")
    return f"{base}/webhooks/sms/status" if base.startswith("https://") else None


async def send_due(conn: sqlite3.Connection, settings: Settings, now: datetime, sender: Sender | None = None,
                   limit: int = 10, fb_sender: FbSender | None = None) -> dict[str, int]:
    """Send due notifications. Returns counts by outcome."""
    counts = {"sent": 0, "retry": 0, "failed": 0, "held": 0}
    channels = []
    if twilio.configured(settings) or sender is not None:
        channels.append("sms")  # otherwise texts wait for Twilio to be set up; don't burn attempts
    if settings.meta_page_access_token.get_secret_value() or fb_sender is not None:
        channels.append("messenger")
    if not channels:
        return counts
    sender = sender or _default_sender
    fb_sender = fb_sender or _default_fb_sender
    rows = conn.execute(
        f"""SELECT * FROM notifications
           WHERE status IN ('pending', 'sending') AND next_attempt_at <= ? AND channel IN ({",".join("?" * len(channels))})
           ORDER BY id LIMIT ?""",
        (to_db(now), *channels, limit),
    ).fetchall()
    for row in rows:
        claimed = conn.execute(
            "UPDATE notifications SET status = 'sending', attempts = attempts + 1, next_attempt_at = ?"
            " WHERE id = ? AND status = ? AND next_attempt_at = ?",
            (to_db(now + LEASE), row["id"], row["status"], row["next_attempt_at"]),
        ).rowcount
        if not claimed:
            continue
        attempts = row["attempts"] + 1
        fb = row["channel"] == "messenger"
        if fb and not owner_accounts.window_open_for(conn, row["recipient"], now):
            conn.execute("UPDATE notifications SET status = 'failed', last_error = ? WHERE id = ?", (WINDOW_CLOSED, row["id"]))
            counts["held"] += 1
            continue
        try:
            if fb:
                sid = await fb_sender(settings, row["recipient"], row["body"], json.loads(row["buttons"]) if row["buttons"] else None)
            else:
                sid = await sender(settings, row["recipient"], row["body"], _status_callback(settings))
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "window_closed", False):
                conn.execute("UPDATE notifications SET status = 'failed', last_error = ? WHERE id = ?", (WINDOW_CLOSED, row["id"]))
                counts["held"] += 1
                continue
            retryable = getattr(exc, "retryable", True)
            final = not retryable or attempts >= MAX_ATTEMPTS
            delay = timedelta(seconds=min(30 * 2 ** (attempts - 1), 1800))
            conn.execute(
                "UPDATE notifications SET status = ?, next_attempt_at = ?, last_error = ? WHERE id = ?",
                ("failed" if final else "pending", to_db(now + delay), str(exc)[:300], row["id"]),
            )
            counts["failed" if final else "retry"] += 1
            log.warning("%s %s to owner failed (attempt %d): %s", row["channel"], row["id"], attempts, exc)
        else:
            conn.execute(
                "UPDATE notifications SET status = 'sent', provider_id = ?, last_error = NULL WHERE id = ?",
                (sid, row["id"]),
            )
            counts["sent"] += 1
    return counts

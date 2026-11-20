"""Sends queued owner SMS (the notifications table) through Twilio with retries.

A row is leased while sending (status 'sending', next_attempt_at = lease expiry) so a crash
mid-send is retried later instead of being lost.
"""

import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from app.config import Settings
from app.integrations import twilio
from app.timeutil import to_db

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
LEASE = timedelta(minutes=5)

Sender = Callable[[Settings, str, str, str | None], Awaitable[str]]


async def _default_sender(settings: Settings, to: str, body: str, status_callback: str | None) -> str:
    return await twilio.send_sms(settings, to, body, status_callback)


def _status_callback(settings: Settings) -> str | None:
    base = settings.public_base_url.rstrip("/")
    return f"{base}/webhooks/sms/status" if base.startswith("https://") else None


async def send_due(conn: sqlite3.Connection, settings: Settings, now: datetime, sender: Sender | None = None,
                   limit: int = 10) -> dict[str, int]:
    """Send due notifications. Returns counts by outcome."""
    counts = {"sent": 0, "retry": 0, "failed": 0}
    if not twilio.configured(settings) and sender is None:
        return counts  # wait for Twilio to be set up; don't burn attempts
    sender = sender or _default_sender
    rows = conn.execute(
        """SELECT * FROM notifications
           WHERE status IN ('pending', 'sending') AND next_attempt_at <= ? ORDER BY id LIMIT ?""",
        (to_db(now), limit),
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
        try:
            sid = await sender(settings, row["recipient"], row["body"], _status_callback(settings))
        except Exception as exc:  # noqa: BLE001
            retryable = getattr(exc, "retryable", True)
            final = not retryable or attempts >= MAX_ATTEMPTS
            delay = timedelta(seconds=min(30 * 2 ** (attempts - 1), 1800))
            conn.execute(
                "UPDATE notifications SET status = ?, next_attempt_at = ?, last_error = ? WHERE id = ?",
                ("failed" if final else "pending", to_db(now + delay), str(exc)[:300], row["id"]),
            )
            counts["failed" if final else "retry"] += 1
            log.warning("SMS %s to owner failed (attempt %d): %s", row["id"], attempts, exc)
        else:
            conn.execute(
                "UPDATE notifications SET status = 'sent', provider_id = ?, last_error = NULL WHERE id = ?",
                (sid, row["id"]),
            )
            counts["sent"] += 1
    return counts

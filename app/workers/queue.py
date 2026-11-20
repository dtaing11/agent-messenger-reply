"""Durable SQLite-backed event queue (webhook_events table).

- Events are persisted before the webhook returns 200, so a crash loses nothing.
- Claiming is atomic (BEGIN IMMEDIATE) and skips customers that already have an event
  in flight, giving per-customer ordering while different customers run concurrently,
  even across several worker processes.
- Failures retry with exponential backoff; stale locks from crashed workers are released.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.database.connection import transaction
from app.timeutil import to_db, utcnow


@dataclass(frozen=True)
class QueuedEvent:
    id: int
    provider: str
    external_event_id: str
    customer_key: str | None
    event_type: str
    payload: dict
    attempts: int


def enqueue(
    conn: sqlite3.Connection,
    provider: str,
    external_event_id: str,
    event_type: str,
    payload: dict,
    customer_key: str | None = None,
    status: str = "pending",
    now: datetime | None = None,
) -> bool:
    """Insert an event, due immediately. Returns False when it is a duplicate delivery."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO webhook_events
             (provider, external_event_id, customer_key, event_type, payload, status, next_attempt_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (provider, external_event_id, customer_key, event_type, json.dumps(payload, separators=(",", ":")), status,
         to_db(now or utcnow())),
    )
    return cur.rowcount == 1


def claim_next(conn: sqlite3.Connection, now: datetime) -> QueuedEvent | None:
    """Claim the oldest due event whose customer has nothing else in flight.

    Within one customer, events run strictly in arrival order: a later event is never
    claimed while an earlier one is still pending (e.g. waiting on a retry backoff)."""
    now_s = to_db(now)
    with transaction(conn):
        row = conn.execute(
            """SELECT e.* FROM webhook_events e
               WHERE e.status = 'pending' AND e.next_attempt_at <= ?
                 AND (e.customer_key IS NULL OR NOT EXISTS (
                       SELECT 1 FROM webhook_events o
                       WHERE o.customer_key = e.customer_key AND o.id != e.id
                         AND (o.status = 'processing' OR (o.status = 'pending' AND o.id < e.id))))
               ORDER BY e.id LIMIT 1""",
            (now_s,),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE webhook_events SET status = 'processing', locked_at = ?, attempts = attempts + 1 WHERE id = ?",
            (now_s, row["id"]),
        )
    return QueuedEvent(
        row["id"], row["provider"], row["external_event_id"], row["customer_key"], row["event_type"],
        json.loads(row["payload"]), row["attempts"] + 1,
    )


def mark_done(conn: sqlite3.Connection, event_id: int, status: str = "done") -> None:
    conn.execute(
        "UPDATE webhook_events SET status = ?, locked_at = NULL, last_error = NULL WHERE id = ?", (status, event_id)
    )


def mark_failed(conn: sqlite3.Connection, event: QueuedEvent, error: str, now: datetime, max_attempts: int) -> None:
    if event.attempts >= max_attempts:
        conn.execute(
            "UPDATE webhook_events SET status = 'failed', locked_at = NULL, last_error = ? WHERE id = ?",
            (error[:2000], event.id),
        )
        return
    delay = min(2 ** event.attempts, 600)  # 2s, 4s, 8s ... capped at 10 minutes
    conn.execute(
        "UPDATE webhook_events SET status = 'pending', locked_at = NULL, last_error = ?, next_attempt_at = ? WHERE id = ?",
        (error[:2000], to_db(now + timedelta(seconds=delay)), event.id),
    )


def release_stale(conn: sqlite3.Connection, now: datetime, lock_timeout_seconds: int) -> int:
    """Return events stuck in 'processing' (worker crashed) to the queue."""
    cur = conn.execute(
        "UPDATE webhook_events SET status = 'pending', locked_at = NULL WHERE status = 'processing' AND locked_at <= ?",
        (to_db(now - timedelta(seconds=lock_timeout_seconds)),),
    )
    return cur.rowcount


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM webhook_events GROUP BY status")}

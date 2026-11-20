"""Conversation sessions: a fresh start for returning customers.

When a customer writes after `session_idle_hours` of silence, the assistant starts a new
session: it only sees messages from then on, and an unfinished cart is set aside (not lost)
so the assistant can ask whether to pick it back up. Orders, the customer's saved name and
phone, and their pickup history are not affected.
"""

import sqlite3
from datetime import datetime, timedelta

from app.database.connection import transaction
from app.services import cart as cart_svc
from app.timeutil import from_db


def maybe_start_session(conn: sqlite3.Connection, customer_id: int, external_message_id: str,
                        idle_hours: float) -> bool:
    """Call after the customer's message is recorded. Returns True if a new session began.
    Safe to call again for the same message (retries)."""
    if idle_hours <= 0:
        return False
    conv = conn.execute("SELECT * FROM conversations WHERE customer_id = ?", (customer_id,)).fetchone()
    msg = conn.execute("SELECT id, created_at FROM messages WHERE external_message_id = ?", (external_message_id,)).fetchone()
    if conv is None or msg is None:
        return False
    if conv["session_start_message_id"] == msg["id"]:
        return True  # already started for this message
    prev = conn.execute(
        "SELECT created_at FROM messages WHERE conversation_id = ? AND id < ? ORDER BY id DESC LIMIT 1",
        (conv["id"], msg["id"]),
    ).fetchone()
    if prev is None or from_db(msg["created_at"]) - from_db(prev["created_at"]) < timedelta(hours=idle_hours):
        return False
    with transaction(conn):
        active = conn.execute("SELECT * FROM carts WHERE customer_id = ? AND status = 'active'", (customer_id,)).fetchone()
        set_aside = conv["set_aside_cart_id"]
        if active is not None:
            has_items = conn.execute("SELECT 1 FROM cart_items WHERE cart_id = ? LIMIT 1", (active["id"],)).fetchone()
            if has_items:
                conn.execute("UPDATE carts SET status = 'abandoned', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                             (active["id"],))
                set_aside = active["id"]
        conn.execute(
            "UPDATE conversations SET session_start_message_id = ?, set_aside_cart_id = ?, state = 'IDLE',"
            " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
            (msg["id"], set_aside, conv["id"]),
        )
    return True


def set_aside_cart(conn: sqlite3.Connection, customer_id: int, tax_rate_bps: int = 0) -> cart_svc.CartView | None:
    conv = conn.execute("SELECT set_aside_cart_id FROM conversations WHERE customer_id = ?", (customer_id,)).fetchone()
    if not conv or not conv["set_aside_cart_id"]:
        return None
    row = conn.execute("SELECT * FROM carts WHERE id = ? AND customer_id = ? AND status = 'abandoned'",
                       (conv["set_aside_cart_id"], customer_id)).fetchone()
    return cart_svc.view_cart_by_id(conn, row["id"], tax_rate_bps) if row else None


def discard_set_aside(conn: sqlite3.Connection, customer_id: int) -> None:
    conn.execute("UPDATE conversations SET set_aside_cart_id = NULL WHERE customer_id = ?", (customer_id,))


def restore_set_aside(conn: sqlite3.Connection, customer_id: int, now: datetime, replace_current: bool = False) -> cart_svc.CartView:
    """Make the set-aside cart the active cart again. A pickup time that has passed is cleared."""
    conv = conn.execute("SELECT set_aside_cart_id FROM conversations WHERE customer_id = ?", (customer_id,)).fetchone()
    aside_id = conv["set_aside_cart_id"] if conv else None
    if not aside_id:
        raise cart_svc.CartError("There's no earlier unfinished order to bring back.")
    with transaction(conn):
        aside = conn.execute("SELECT * FROM carts WHERE id = ? AND customer_id = ? AND status = 'abandoned'",
                             (aside_id, customer_id)).fetchone()
        if aside is None:
            conn.execute("UPDATE conversations SET set_aside_cart_id = NULL WHERE customer_id = ?", (customer_id,))
            raise cart_svc.CartError("That earlier order can't be brought back anymore.")
        active = conn.execute("SELECT * FROM carts WHERE customer_id = ? AND status = 'active'", (customer_id,)).fetchone()
        if active is not None:
            has_items = conn.execute("SELECT 1 FROM cart_items WHERE cart_id = ? LIMIT 1", (active["id"],)).fetchone()
            if has_items and not replace_current:
                raise cart_svc.CartError("The customer already has items in a new order. Ask whether to replace them "
                                         "with the earlier order, then call again with replace_current=true.")
            conn.execute("UPDATE carts SET status = 'abandoned' WHERE id = ?", (active["id"],))
        past = aside["pickup_time"] and from_db(aside["pickup_time"]) <= now
        conn.execute(
            "UPDATE carts SET status = 'active', pickup_time = CASE WHEN ? THEN NULL ELSE pickup_time END,"
            " version = version + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
            (1 if past else 0, aside_id),
        )
        conn.execute("UPDATE conversations SET set_aside_cart_id = NULL WHERE customer_id = ?", (customer_id,))
    return cart_svc.view_cart(conn, customer_id)

"""All-or-nothing cart changes for each customer message the assistant handles.

Before the assistant runs, the customer's cart is saved under the message id. If handling fails
part way (an unexpected error or a crash) the retry first puts the cart back the way it was, so a
tool call that already ran ("add 2 glazed") can't run twice. If the AI service fails, the cart is
put back too. Once the reply is saved, the snapshot is dropped.
"""

import json
import sqlite3
from datetime import datetime, timedelta

from app.database.connection import transaction
from app.timeutil import to_db


def _snapshot(conn: sqlite3.Connection, customer_id: int) -> dict:
    cart = conn.execute("SELECT * FROM carts WHERE customer_id = ? AND status = 'active'", (customer_id,)).fetchone()
    conv = conn.execute("SELECT set_aside_cart_id, state FROM conversations WHERE customer_id = ?", (customer_id,)).fetchone()
    snap: dict = {"cart": None, "set_aside_cart_id": conv["set_aside_cart_id"] if conv else None,
                  "state": conv["state"] if conv else None}
    if cart is not None:
        items = conn.execute("SELECT item_id, quantity, modifiers, details, added_at FROM cart_items WHERE cart_id = ? ORDER BY id",
                             (cart["id"],)).fetchall()
        snap["cart"] = {"id": cart["id"], "pickup_time": cart["pickup_time"], "customer_name": cart["customer_name"],
                        "customer_phone": cart["customer_phone"], "notes": cart["notes"],
                        "items": [list(i) for i in items]}
    return snap


def _restore(conn: sqlite3.Connection, customer_id: int, snap: dict) -> None:
    with transaction(conn):
        saved = snap["cart"]
        current = conn.execute("SELECT id FROM carts WHERE customer_id = ? AND status = 'active'", (customer_id,)).fetchone()
        if current is not None and (saved is None or current["id"] != saved["id"]):
            conn.execute("UPDATE carts SET status = 'abandoned' WHERE id = ?", (current["id"],))
        if saved is not None:
            # A new version, so a summary shown during the failed attempt can't be confirmed.
            conn.execute(
                """UPDATE carts SET status = 'active', pickup_time = ?, customer_name = ?, customer_phone = ?, notes = ?,
                     version = version + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?""",
                (saved["pickup_time"], saved["customer_name"], saved["customer_phone"], saved["notes"], saved["id"]),
            )
            conn.execute("DELETE FROM cart_items WHERE cart_id = ?", (saved["id"],))
            conn.executemany(
                "INSERT INTO cart_items (cart_id, item_id, quantity, modifiers, details, added_at) VALUES (?, ?, ?, ?, ?, ?)",
                [(saved["id"], *item[:4], item[4] if len(item) > 4 else None) for item in saved["items"]])
        if snap.get("state"):
            conn.execute("UPDATE conversations SET set_aside_cart_id = ?, state = ? WHERE customer_id = ?",
                         (snap["set_aside_cart_id"], snap["state"], customer_id))


def begin(conn: sqlite3.Connection, customer_id: int, message_id: str) -> bool:
    """Call before the assistant handles a message. Returns True if an unfinished earlier attempt
    was found and the cart was put back first."""
    row = conn.execute("SELECT snapshot FROM agent_turns WHERE message_id = ?", (message_id,)).fetchone()
    if row is not None:
        _restore(conn, customer_id, json.loads(row["snapshot"]))
        return True
    conn.execute("INSERT INTO agent_turns (message_id, customer_id, snapshot) VALUES (?, ?, ?)",
                 (message_id, customer_id, json.dumps(_snapshot(conn, customer_id))))
    return False


def rollback(conn: sqlite3.Connection, customer_id: int, message_id: str) -> None:
    """Undo this message's cart changes (the AI failed part way)."""
    row = conn.execute("SELECT snapshot FROM agent_turns WHERE message_id = ?", (message_id,)).fetchone()
    if row is not None:
        _restore(conn, customer_id, json.loads(row["snapshot"]))
    finish(conn, message_id)


def finish(conn: sqlite3.Connection, message_id: str, now: datetime | None = None) -> None:
    conn.execute("DELETE FROM agent_turns WHERE message_id = ?", (message_id,))
    if now is not None:
        conn.execute("DELETE FROM agent_turns WHERE created_at < ?", (to_db(now - timedelta(days=2)),))

"""Customers, conversations and per-customer message history."""

import sqlite3

from app.agent.state_machine import ConversationState, can_change_conversation


def get_or_create_customer(conn: sqlite3.Connection, page_id: str, psid: str) -> sqlite3.Row:
    conn.execute("INSERT OR IGNORE INTO customers (page_id, facebook_psid) VALUES (?, ?)", (page_id, psid))
    customer = conn.execute(
        "SELECT * FROM customers WHERE page_id = ? AND facebook_psid = ?", (page_id, psid)
    ).fetchone()
    conn.execute("INSERT OR IGNORE INTO conversations (customer_id) VALUES (?)", (customer["id"],))
    return customer


def get_conversation(conn: sqlite3.Connection, customer_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM conversations WHERE customer_id = ?", (customer_id,)).fetchone()


def record_message(
    conn: sqlite3.Connection, customer_id: int, role: str, content: str, external_id: str | None = None
) -> bool:
    """Append to history. Returns False if this external message id was already stored."""
    conv = get_conversation(conn, customer_id)
    cur = conn.execute(
        "INSERT OR IGNORE INTO messages (conversation_id, external_message_id, role, content) VALUES (?, ?, ?, ?)",
        (conv["id"], external_id, role, content),
    )
    if role == "customer":
        conn.execute(
            "UPDATE conversations SET last_customer_message_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
            " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
            (conv["id"],),
        )
    return cur.rowcount == 1


def recent_messages(conn: sqlite3.Connection, customer_id: int, limit: int = 20) -> list[sqlite3.Row]:
    conv = get_conversation(conn, customer_id)
    rows = conn.execute(
        "SELECT role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
        (conv["id"], limit),
    ).fetchall()
    return list(reversed(rows))


def set_state(conn: sqlite3.Connection, customer_id: int, target: ConversationState) -> None:
    conv = get_conversation(conn, customer_id)
    if not can_change_conversation(conv["state"], target):
        raise ValueError(f"conversation cannot move from {conv['state']} to {target}")
    conn.execute(
        "UPDATE conversations SET state = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
        (str(target), conv["id"]),
    )


def set_bot_enabled(conn: sqlite3.Connection, customer_id: int, enabled: bool, reason: str | None = None) -> None:
    """HUMAN_HANDOFF flag: when disabled, the bot stays silent until the owner resumes it."""
    conn.execute(
        "UPDATE conversations SET bot_enabled = ?, handoff_reason = ?,"
        " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE customer_id = ?",
        (1 if enabled else 0, None if enabled else reason, customer_id),
    )

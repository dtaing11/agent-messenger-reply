"""Outgoing notifications: owner SMS rows (sent by the notification worker) and
customer Messenger updates (queued as internal events for the message worker)."""

import sqlite3
from datetime import datetime, timedelta

from app.agent.state_machine import OrderStatus
from app.config import Settings
from app.services.business import BusinessInfo
from app.timeutil import from_db, local
from app.workers import queue

MESSENGER_WINDOW = timedelta(hours=24)


def alert_owners(conn: sqlite3.Connection, settings: Settings, kind: str, body: str, key: str,
                 order_id: int | None = None) -> int:
    """Queue one SMS per owner phone. `key` makes it idempotent across retries."""
    n = 0
    for phone in settings.owner_phone_numbers:
        n += conn.execute(
            "INSERT OR IGNORE INTO notifications (order_id, type, recipient, body, idempotency_key) VALUES (?, ?, ?, ?, ?)",
            (order_id, kind, phone, body[:1500], f"{key}:{phone}"),
        ).rowcount
    return n


def customer_label(conn: sqlite3.Connection, customer_id: int) -> str:
    row = conn.execute(
        """SELECT c.name, c.facebook_psid,
                  (SELECT customer_name FROM carts WHERE customer_id = c.id AND customer_name IS NOT NULL
                   ORDER BY id DESC LIMIT 1) AS cart_name
           FROM customers c WHERE c.id = ?""",
        (customer_id,),
    ).fetchone()
    if row is None:
        return "a customer"
    name = row["name"] or row["cart_name"]
    return f"{name} (Messenger ...{row['facebook_psid'][-4:]})" if name else f"Messenger customer ...{row['facebook_psid'][-4:]}"


def handoff_sms(conn: sqlite3.Connection, customer_id: int, reason: str) -> str:
    return (
        f"Town Donut bot: {customer_label(conn, customer_id)} needs a person.\n"
        f"Reason: {reason[:160]}\n"
        "Please reply to them from the Facebook Page inbox. The bot stays quiet for this customer "
        "until you resume it in the portal."
    )


def within_window(conn: sqlite3.Connection, customer_id: int, now: datetime) -> bool:
    row = conn.execute("SELECT last_customer_message_at FROM conversations WHERE customer_id = ?", (customer_id,)).fetchone()
    return bool(row and row["last_customer_message_at"] and now - from_db(row["last_customer_message_at"]) < MESSENGER_WINDOW)


def order_update_text(order: sqlite3.Row, status: OrderStatus, biz: BusinessInfo) -> str | None:
    pickup = local(from_db(order["pickup_time"]), biz.timezone)
    when = f"{pickup:%a %b} {pickup.day} at {pickup:%-I:%M %p}"
    phone = biz.raw.get("phone", "")
    if status == OrderStatus.ACCEPTED:
        return "\n".join([
            f"Good news! Your order #{order['id']} is confirmed for pickup {when}.",
            "",
            *biz.pickup_location_lines(),
            "",
            "Prices are before tax, and you'll pay at pickup. See you soon!",
        ])
    if status == OrderStatus.READY:
        return "\n".join([f"Your order #{order['id']} is ready for pickup!", "", *biz.pickup_location_lines()])
    if status == OrderStatus.REJECTED:
        return (f"Sorry, we aren't able to take order #{order['id']}. "
                f"Please call us at {phone} and we'll be happy to help.")
    if status == OrderStatus.EXPIRED:
        return (f"Sorry, our team couldn't confirm order #{order['id']} in time, so it wasn't placed. "
                f"Please message us again or call {phone}.")
    if status == OrderStatus.CANCELLED:
        return f"Order #{order['id']} has been cancelled."
    return None


def queue_customer_update(conn: sqlite3.Connection, order: sqlite3.Row, status: OrderStatus, biz: BusinessInfo,
                          now: datetime) -> bool:
    """Queue a Messenger message about this order for the message worker. Idempotent per order+status."""
    text = order_update_text(order, status, biz)
    if text is None:
        return False
    cust = conn.execute("SELECT page_id, facebook_psid FROM customers WHERE id = ?", (order["customer_id"],)).fetchone()
    if cust is None:
        return False
    return queue.enqueue(
        conn, "internal", f"order:{order['id']}:{status}", "customer_update",
        {"page_id": cust["page_id"], "psid": cust["facebook_psid"], "text": text, "order_id": order["id"]},
        customer_key=f"{cust['page_id']}:{cust['facebook_psid']}", now=now,
    )

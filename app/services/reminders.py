"""A nudge for customers who picked items but never gave the pickup name or phone number.

Owner, 2026-11-20: if a customer has agreed on an order but hasn't sent the name and phone
number of the person picking it up, remind them once (about an hour later) that the order
can't be placed without them. The store doesn't call customers about orders, so the reminder
is the only follow-up.

Rules: once per cart; about CONTACT_REMINDER_MINUTES after the customer's last message, or
CONTACT_REMINDER_SOON_MINUTES when the chosen pickup time is closer than that; never while the
bot is paused or staff have the chat, never for an abandoned or sent cart, never after the
pickup time, and only inside Meta's 24-hour window. The message goes through the durable
queue, and the worker checks everything again just before sending.
"""

import sqlite3
from datetime import datetime, timedelta

from app.config import Settings
from app.services import cart as cart_svc
from app.services import notify
from app.timeutil import from_db, to_db
from app.workers import queue

EVENT_TYPE = "contact_reminder"
# A reminder that's this late (app was down, staff handed the chat back hours later) is skipped.
LATE_AFTER = timedelta(hours=3)


def missing_contact(view: cart_svc.CartView, require_phone: bool) -> list[str]:
    """What still keeps the order from being placed, when a name or phone number is missing."""
    who = []
    if not view.customer_name:
        who.append("name")
    if require_phone and not view.customer_phone:
        who.append("phone")
    if view.is_empty or not who:
        return []
    return (["pickup time"] if view.pickup_time is None else []) + who


def reminder_text(missing: list[str]) -> str:
    who = [m for m in missing if m in ("name", "phone")]
    if who == ["name", "phone"]:
        person = "the name and phone number of the person picking it up"
    elif who == ["name"]:
        person = "the name of the person picking it up"
    else:
        person = "the phone number for the person picking it up"
    needed = f"a pickup time, and {person}" if "pickup time" in missing else person
    return f"Just a reminder: we can't place your order until we have {needed}. Reply here whenever you're ready!"


def _delay(settings: Settings, last_message: datetime, pickup: datetime | None) -> timedelta:
    normal = timedelta(minutes=settings.contact_reminder_minutes)
    if pickup is not None and pickup <= last_message + normal:
        return min(normal, timedelta(minutes=settings.contact_reminder_soon_minutes))
    return normal


def still_due(conn: sqlite3.Connection, settings: Settings, cart_id: int, now: datetime) -> tuple[bool, list[str]]:
    """Whether a reminder for this cart should go out right now, and what's missing."""
    cart = conn.execute(
        """SELECT c.*, cv.bot_enabled, cv.last_customer_message_at FROM carts c
           JOIN conversations cv ON cv.customer_id = c.customer_id WHERE c.id = ?""", (cart_id,)).fetchone()
    if (cart is None or cart["status"] != "active" or not cart["bot_enabled"] or settings.bot_mode != "live"
            or not cart["last_customer_message_at"]):
        return False, []
    if cart["pickup_time"] and from_db(cart["pickup_time"]) <= now:
        return False, []
    if not notify.within_window(conn, cart["customer_id"], now):
        return False, []
    missing = missing_contact(cart_svc.view_cart_by_id(conn, cart_id), settings.require_customer_phone)
    return bool(missing), missing


def queue_contact_reminders(conn: sqlite3.Connection, settings: Settings, now: datetime) -> list[int]:
    """Queue the reminder for each cart that's due. Returns the cart ids."""
    if not settings.contact_reminder_minutes or settings.bot_mode != "live":
        return []
    soonest = min(settings.contact_reminder_minutes, settings.contact_reminder_soon_minutes)
    rows = conn.execute(
        """SELECT c.id, c.customer_id, c.pickup_time, cv.last_customer_message_at, cu.page_id, cu.facebook_psid
           FROM carts c
           JOIN conversations cv ON cv.customer_id = c.customer_id
           JOIN customers cu ON cu.id = c.customer_id
           WHERE c.status = 'active' AND c.contact_reminder_at IS NULL AND cv.bot_enabled = 1
             AND cv.last_customer_message_at IS NOT NULL AND cv.last_customer_message_at <= ?
             AND EXISTS (SELECT 1 FROM cart_items ci WHERE ci.cart_id = c.id)""",
        (to_db(now - timedelta(minutes=soonest)),),
    ).fetchall()
    queued = []
    for r in rows:
        last = from_db(r["last_customer_message_at"])
        pickup = from_db(r["pickup_time"]) if r["pickup_time"] else None
        due = last + _delay(settings, last, pickup)
        if now < due or now - due > LATE_AFTER:
            continue
        due_now, missing = still_due(conn, settings, r["id"], now)
        if not due_now:
            continue
        if not conn.execute("UPDATE carts SET contact_reminder_at = ? WHERE id = ? AND contact_reminder_at IS NULL",
                            (to_db(now), r["id"])).rowcount:
            continue
        queue.enqueue(
            conn, "internal", f"cart:{r['id']}:contact_reminder", EVENT_TYPE,
            {"page_id": r["page_id"], "psid": r["facebook_psid"], "cart_id": r["id"],
             "last_customer_message_at": r["last_customer_message_at"]},
            customer_key=f"{r['page_id']}:{r['facebook_psid']}", now=now,
        )
        queued.append(r["id"])
    return queued

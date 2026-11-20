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

A Special Design Donut must be confirmed by 3 AM on the pickup day (owner, 2026-11-20), so for
those the reminder names that deadline, comes sooner when the deadline is close, and isn't sent
once it has passed.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.config import Settings
from app.services import cart as cart_svc
from app.services import notify
from app.services.business import BusinessInfo, hour_text, notice_deadline
from app.timeutil import from_db, local, to_db
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


@dataclass
class Due:
    due: bool
    missing: list[str] = field(default_factory=list)
    deadline: datetime | None = None  # Special Design Donut: confirm by 3 AM on the pickup day
    item: str | None = None


def _deadline(view: cart_svc.CartView, biz: BusinessInfo) -> tuple[datetime | None, str | None]:
    line = max(view.lines, key=lambda l: l.min_days_ahead, default=None)
    if line is None or not line.min_days_ahead or view.pickup_time is None:
        return None, None
    return notice_deadline(biz, local(view.pickup_time, biz.timezone).date(), line.min_days_ahead), line.name


def reminder_text(missing: list[str], deadline: datetime | None = None, item: str | None = None) -> str:
    who = [m for m in missing if m in ("name", "phone")]
    if who == ["name", "phone"]:
        person = "the name and phone number of the person picking it up"
    elif who == ["name"]:
        person = "the name of the person picking it up"
    else:
        person = "the phone number for the person picking it up"
    needed = f"a pickup time, and {person}" if "pickup time" in missing else person
    text = f"Just a reminder: we can't place your order until we have {needed}."
    if deadline is not None:
        day = f"{deadline:%A}, {deadline:%b} {deadline.day}"
        text += (f" We need it by {hour_text(deadline.hour)} on {day}, or we won't be able to make your "
                 f"{item or 'order'} for that day.")
    return text + " Reply here whenever you're ready!"


def _delay(settings: Settings, last_message: datetime, urgent_at: datetime | None) -> timedelta:
    """The usual wait, or the short one when the pickup time (or a design donut deadline) is close."""
    normal = timedelta(minutes=settings.contact_reminder_minutes)
    if urgent_at is not None and urgent_at <= last_message + normal:
        return min(normal, timedelta(minutes=settings.contact_reminder_soon_minutes))
    return normal


def still_due(conn: sqlite3.Connection, settings: Settings, biz: BusinessInfo, cart_id: int, now: datetime) -> Due:
    """Whether a reminder for this cart should go out right now, and what to say."""
    cart = conn.execute(
        """SELECT c.*, cv.bot_enabled, cv.last_customer_message_at FROM carts c
           JOIN conversations cv ON cv.customer_id = c.customer_id WHERE c.id = ?""", (cart_id,)).fetchone()
    if (cart is None or cart["status"] != "active" or not cart["bot_enabled"] or settings.bot_mode != "live"
            or not cart["last_customer_message_at"]):
        return Due(False)
    if cart["pickup_time"] and from_db(cart["pickup_time"]) <= now:
        return Due(False)
    if not notify.within_window(conn, cart["customer_id"], now):
        return Due(False)
    view = cart_svc.view_cart_by_id(conn, cart_id)
    deadline, item = _deadline(view, biz)
    if deadline is not None and now >= deadline:
        return Due(False)  # too late for that day: when they write again, Sprinkle explains and offers another
    if deadline is not None:
        days, _ = view.advance_notice()
        ordered = local(view.notice_ordered_at(now), biz.timezone).date()
        if local(view.pickup_time, biz.timezone).date() < ordered + timedelta(days=days):
            return Due(False)  # ordered for the same day: it can't be made, so there's nothing to remind about
    missing = missing_contact(view, settings.require_customer_phone)
    return Due(bool(missing), missing, deadline, item)


def queue_contact_reminders(conn: sqlite3.Connection, settings: Settings, biz: BusinessInfo, now: datetime) -> list[int]:
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
        deadline, _ = _deadline(cart_svc.view_cart_by_id(conn, r["id"]), biz)
        urgent_at = min((t for t in (pickup, deadline) if t is not None), default=None)
        due = last + _delay(settings, last, urgent_at)
        if now < due or now - due > LATE_AFTER:
            continue
        if not still_due(conn, settings, biz, r["id"], now).due:
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

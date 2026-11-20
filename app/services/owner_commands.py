"""Owner actions on orders, by text message (from an owner phone, via Twilio) or the portal.

  YES 12 / NO 12      accept or decline a pending order
  READY 12            order is ready (customer gets a Messenger message)
  PICKED 12           customer picked it up          (also: DONE 12, PICKED UP 12)
  NOSHOW 12           customer never came            (also: NO SHOW 12, MISSED 12)
  ORDERS              list open orders
  PAUSE / RESUME      stop or start automatic Messenger replies
  HELP                this list

The order number may be left off when exactly one order fits. No-shows are an owner-only
record: the customer is never told, and the assistant never sees it.
"""

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from app.agent.state_machine import InvalidTransition, OrderStatus, can_transition
from app.config import Settings
from app.database.connection import transaction
from app.integrations.messenger import split_message
from app.services import customers, notify
from app.services import order as order_svc
from app.services.business import BusinessInfo
from app.services.phone import display as phone_display
from app.services.pricing import money
from app.settings import runtime
from app.timeutil import from_db, local, to_db

HELP = ("Commands: YES 12 (accept), NO 12 (decline), READY 12, PICKED 12 (picked up), NOSHOW 12 (didn't come), "
        "ORDERS (open orders), PAUSE or RESUME (bot replies).")

ACTIONS: dict[str, OrderStatus] = {
    "YES": OrderStatus.ACCEPTED, "Y": OrderStatus.ACCEPTED, "ACCEPT": OrderStatus.ACCEPTED, "OK": OrderStatus.ACCEPTED,
    "NO": OrderStatus.REJECTED, "N": OrderStatus.REJECTED, "DECLINE": OrderStatus.REJECTED, "REJECT": OrderStatus.REJECTED,
    "READY": OrderStatus.READY,
    "PICKED": OrderStatus.COMPLETED, "PICKEDUP": OrderStatus.COMPLETED, "DONE": OrderStatus.COMPLETED,
    "COMPLETE": OrderStatus.COMPLETED, "COMPLETED": OrderStatus.COMPLETED,
    "NOSHOW": OrderStatus.NO_SHOW, "MISSED": OrderStatus.NO_SHOW,
}
OPEN = (OrderStatus.PENDING_ACCEPTANCE, OrderStatus.ACCEPTED, OrderStatus.PREPARING, OrderStatus.READY)
DONE_WORDS = {
    OrderStatus.ACCEPTED: "accepted",
    OrderStatus.REJECTED: "declined",
    OrderStatus.READY: "marked ready",
    OrderStatus.COMPLETED: "marked picked up",
    OrderStatus.NO_SHOW: "marked as a no-show",
}


@dataclass(frozen=True)
class OwnerSender:
    """Who sent an owner command: an owner phone (SMS) or a linked Messenger account."""
    channel: str  # "sms" | "messenger" | "portal"
    address: str  # E.164 phone, or PSID
    label: str = ""

    @classmethod
    def phone(cls, number: str) -> "OwnerSender":
        return cls("sms", number)

    @property
    def actor(self) -> str:
        return f"owner_sms:{self.address[-4:]}" if self.channel == "sms" else f"owner_fb:{self.label or self.address[-4:]}"

    @property
    def who(self) -> str:
        return f"the phone ending {self.address[-4:]}" if self.channel == "sms" else f"{self.label or 'an owner'} on Messenger"


def _open_orders(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    marks = ",".join("?" * len(OPEN))
    return conn.execute(f"SELECT * FROM orders WHERE status IN ({marks}) ORDER BY pickup_time", [str(s) for s in OPEN]).fetchall()


def _list_orders(conn: sqlite3.Connection, biz: BusinessInfo) -> str:
    rows = _open_orders(conn)
    if not rows:
        return "No open orders."
    lines = []
    for r in rows[:10]:
        t = local(from_db(r["pickup_time"]), biz.timezone)
        status = r["status"].replace("_", " ").lower()
        lines.append(f"#{r['id']} {r['customer_name']} {t:%b} {t.day} {t:%-I:%M %p} {money(r['subtotal_cents'])} - {status}")
    if len(rows) > 10:
        lines.append(f"...and {len(rows) - 10} more")
    return "\n".join(lines)


def apply_status(conn: sqlite3.Connection, settings: Settings, biz: BusinessInfo, order_id: int, target: OrderStatus,
                 actor: str, now: datetime, from_phone: str | None = None,
                 sender: OwnerSender | None = None) -> tuple[bool, str]:
    """Change an order's status for an owner, tell the customer when that applies, and let the
    other owners know. Returns (ok, message for the owner)."""
    if sender is None and from_phone:
        sender = OwnerSender.phone(from_phone)
    try:
        order = order_svc.transition_order(conn, order_id, target, actor=actor)
    except InvalidTransition as exc:
        current = exc.current.replace("_", " ").lower()
        return False, f"Order #{order_id} is {current}, so it can't be {DONE_WORDS[target]}."
    except order_svc.OrderError as exc:
        return False, str(exc)

    reply = f"Order #{order_id} {DONE_WORDS[target]}."
    if target == OrderStatus.NO_SHOW:
        h = customers.pickup_history(conn, order["customer_id"], order["customer_phone"])
        reply += (f" {order['customer_name']} now has {h.no_shows} missed pickup(s) on record. "
                  "You'll get a heads-up if they order again. (The customer isn't told.)")
    elif notify.order_update_text(order, target, biz):
        if notify.within_window(conn, order["customer_id"], now):
            notify.queue_customer_update(conn, order, target, biz, now)
            reply += " The customer will get a Messenger message."
        else:
            phone = phone_display(order["customer_phone"])
            reply += (" The customer last wrote over 24 hours ago, so Messenger won't let us message them automatically."
                      + (f" Their phone: {phone}." if phone else ""))

    # Keep the other owners in the loop, so two people don't act on the same order.
    who = sender.who if sender else "the portal"
    notify.alert_owners(conn, settings, "owner_fyi", f"FYI: order #{order_id} was {DONE_WORDS[target]} by {who}.",
                        f"order:{order_id}:{target}:fyi", order_id=order_id,
                        exclude=sender.address if sender else None, now=now)
    return True, reply


def _parse(body: str) -> list[str]:
    text = re.sub(r"\bNO[\s-]+SHOW\b", "NOSHOW", (body or "").upper())
    text = re.sub(r"\bPICKED[\s-]+UP\b", "PICKED", text)
    return re.findall(r"[A-Z]+|\d+", text)


def handle(conn: sqlite3.Connection, settings: Settings, biz: BusinessInfo, sender: str | OwnerSender, body: str,
           now: datetime) -> str:
    """Apply one owner command (a text, or a Messenger message from a linked owner) and return the reply."""
    if isinstance(sender, str):
        sender = OwnerSender.phone(sender)
    words = _parse(body)
    if not words:
        return HELP
    cmd = words[0]
    if cmd in ("HELP", "COMMANDS", "MENU"):
        return HELP
    if cmd == "KEEPALIVE":
        return "Thanks! Order alerts stay on here for another 24 hours."
    if cmd in ("ORDERS", "LIST", "OPEN"):
        return _list_orders(conn, biz)
    if cmd == "PAUSE":
        # Not STOP/START: those are carrier opt-out keywords, and Twilio would stop texting this phone.
        runtime.set_values(conn, {"bot_mode": "paused"}, sender.actor)
        return "Bot paused. Messages are saved, but customers get no automatic replies. Text RESUME to turn it back on."
    if cmd in ("RESUME", "LIVE"):
        if not settings.meta_app_secret.get_secret_value():
            return "Can't turn the bot on yet: add the Meta App secret in the portal first."
        runtime.set_values(conn, {"bot_mode": "live"}, sender.actor)
        return "Bot is live and replying to customers again."

    target = ACTIONS.get(cmd)
    if target is None:
        return "Sorry, I didn't understand that. " + HELP
    numbers = [int(w) for w in words[1:] if w.isdigit()]
    if numbers:
        order_id = numbers[0]
    else:
        fits = [r for r in _open_orders(conn) if can_transition(r["status"], target)]
        if len(fits) != 1:
            return (f"Which order? Reply {cmd} and the order number, e.g. {cmd} 12." if fits
                    else f"No order can be {DONE_WORDS[target]} right now.")
        order_id = fits[0]["id"]
    _, reply = apply_status(conn, settings, biz, order_id, target, sender.actor, now, sender=sender)
    return reply


def pickup_check_sms(order: sqlite3.Row, biz: BusinessInfo) -> str:
    t = local(from_db(order["pickup_time"]), biz.timezone)
    phone = phone_display(order["customer_phone"])
    return (f"Did {order['customer_name']}{f' ({phone})' if phone else ''} pick up order #{order['id']} "
            f"(pickup {t:%-I:%M %p})? Reply PICKED {order['id']} or NOSHOW {order['id']}.")


def queue_pickup_checks(conn: sqlite3.Connection, settings: Settings, biz: BusinessInfo, now: datetime) -> list[int]:
    """After pickup time (+ a grace period), ask the owners once whether the order was picked up."""
    if not settings.pickup_check_after_minutes:
        return []
    cutoff = to_db(now - timedelta(minutes=settings.pickup_check_after_minutes))
    rows = conn.execute(
        """SELECT * FROM orders WHERE status IN (?, ?, ?) AND pickup_check_sent_at IS NULL AND pickup_time <= ?""",
        (str(OrderStatus.ACCEPTED), str(OrderStatus.PREPARING), str(OrderStatus.READY), cutoff),
    ).fetchall()
    asked = []
    for o in rows:
        if conn.execute("UPDATE orders SET pickup_check_sent_at = ? WHERE id = ? AND pickup_check_sent_at IS NULL",
                        (to_db(now), o["id"])).rowcount:
            notify.alert_owners(conn, settings, "pickup_check", pickup_check_sms(o, biz), f"order:{o['id']}:pickup_check",
                                order_id=o["id"], now=now,
                                buttons=[[f"Picked up #{o['id']}", f"PICKED {o['id']}"], [f"No-show #{o['id']}", f"NOSHOW {o['id']}"]])
            asked.append(o["id"])
    return asked


REMINDER_STATUSES = (OrderStatus.ACCEPTED, OrderStatus.PREPARING)
SMS_PART = 1500  # Twilio sends up to 1600 characters as one text; leave room for "(1/2)"


def _reminder_order_lines(conn: sqlite3.Connection, order: sqlite3.Row, biz: BusinessInfo) -> list[str]:
    t = local(from_db(order["pickup_time"]), biz.timezone)
    phone = phone_display(order["customer_phone"])
    lines = [f"#{order['id']} {t:%-I:%M %p} - {order['customer_name']}" + (f", {phone}" if phone else "")]
    for it in conn.execute("SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order["id"],)):
        mods = json.loads(it["modifiers_snapshot"])
        lines.append(f"{it['quantity']} x {it['item_name_snapshot']}" + (f" ({', '.join(m['name'] for m in mods)})" if mods else ""))
        lines += order_svc._detail_lines(json.loads(it["details_snapshot"]), indent="  ")
    if order["notes"]:
        lines.append(f"Notes: {order['notes']}")
    lines.append(f"Subtotal {money(order['subtotal_cents'])} (before tax)")
    return lines


def day_reminder_texts(conn: sqlite3.Connection, orders: list[sqlite3.Row], biz: BusinessInfo, day: datetime) -> list[str]:
    n = len(orders)
    head = (f"Good morning! Reminder: {n} order{'s' if n != 1 else ''} placed ahead for pickup today "
            f"({day:%a %b} {day.day}):")
    lines = [head]
    for o in orders:
        lines += ["", *_reminder_order_lines(conn, o, biz)]
    parts = split_message("\n".join(lines), SMS_PART)
    return parts if len(parts) == 1 else [f"({i}/{len(parts)}) {p}" for i, p in enumerate(parts, 1)]


def queue_day_reminders(conn: sqlite3.Connection, settings: Settings, biz: BusinessInfo, now: datetime) -> list[int]:
    """From `owner_reminder_hour` (3 AM) on the pickup day, text every owner phone the accepted
    orders for today that were placed on an earlier day, once. Runs late if the computer was off."""
    hour = settings.owner_reminder_hour
    if hour < 0 or not notify.owner_recipients(conn, settings, now):
        return []
    now_local = local(now, biz.timezone)
    if now_local.time() < time(hour):
        return []
    start = datetime.combine(now_local.date(), time(0), tzinfo=biz.tz)
    marks = ",".join("?" * len(REMINDER_STATUSES))
    rows = conn.execute(
        f"""SELECT * FROM orders WHERE status IN ({marks}) AND day_reminder_sent_at IS NULL
              AND pickup_time > ? AND pickup_time < ? AND created_at < ? ORDER BY pickup_time, id""",
        (*[str(s) for s in REMINDER_STATUSES], to_db(now), to_db(start + timedelta(days=1)), to_db(start)),
    ).fetchall()
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    texts = day_reminder_texts(conn, rows, biz, now_local)
    with transaction(conn):
        claimed = conn.execute(
            f"UPDATE orders SET day_reminder_sent_at = ? WHERE id IN ({','.join('?' * len(ids))}) AND day_reminder_sent_at IS NULL",
            (to_db(now), *ids),
        ).rowcount
        if claimed != len(ids):
            raise RuntimeError("day reminder raced with another worker")  # rolled back; next pass retries
        key = f"day_reminder:{now_local:%Y-%m-%d}:{ids[0]}-{ids[-1]}"
        for i, body in enumerate(texts, 1):
            notify.alert_owners(conn, settings, "day_reminder", body, f"{key}:{i}", now=now)
    return ids

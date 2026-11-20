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

import re
import sqlite3
from datetime import datetime, timedelta

from app.agent.state_machine import InvalidTransition, OrderStatus, can_transition
from app.config import Settings
from app.integrations.twilio import digits
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
                 actor: str, now: datetime, from_phone: str | None = None) -> tuple[bool, str]:
    """Change an order's status for an owner, tell the customer when that applies, and let the
    other owner phones know. Returns (ok, message for the owner)."""
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

    # Keep the other owner phones in the loop, so two people don't act on the same order.
    who = f"the phone ending {from_phone[-4:]}" if from_phone else "the portal"
    for p in settings.owner_phone_numbers:
        if from_phone and digits(p) == digits(from_phone):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO notifications (order_id, type, recipient, body, idempotency_key) VALUES (?, 'owner_fyi', ?, ?, ?)",
            (order_id, p, f"FYI: order #{order_id} was {DONE_WORDS[target]} from {who}.", f"order:{order_id}:{target}:fyi:{p}"),
        )
    return True, reply


def _parse(body: str) -> list[str]:
    text = re.sub(r"\bNO[\s-]+SHOW\b", "NOSHOW", (body or "").upper())
    text = re.sub(r"\bPICKED[\s-]+UP\b", "PICKED", text)
    return re.findall(r"[A-Z]+|\d+", text)


def handle(conn: sqlite3.Connection, settings: Settings, biz: BusinessInfo, from_phone: str, body: str,
           now: datetime) -> str:
    """Apply one owner text command and return the SMS reply."""
    words = _parse(body)
    if not words:
        return HELP
    cmd = words[0]
    if cmd in ("HELP", "COMMANDS", "MENU"):
        return HELP
    if cmd in ("ORDERS", "LIST", "OPEN"):
        return _list_orders(conn, biz)
    if cmd == "PAUSE":
        # Not STOP/START: those are carrier opt-out keywords, and Twilio would stop texting this phone.
        runtime.set_values(conn, {"bot_mode": "paused"}, f"sms:{from_phone[-4:]}")
        return "Bot paused. Messages are saved, but customers get no automatic replies. Text RESUME to turn it back on."
    if cmd in ("RESUME", "LIVE"):
        if not settings.meta_app_secret.get_secret_value():
            return "Can't turn the bot on yet: add the Meta App secret in the portal first."
        runtime.set_values(conn, {"bot_mode": "live"}, f"sms:{from_phone[-4:]}")
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
    _, reply = apply_status(conn, settings, biz, order_id, target, f"owner_sms:{from_phone[-4:]}", now, from_phone)
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
                                order_id=o["id"])
            asked.append(o["id"])
    return asked

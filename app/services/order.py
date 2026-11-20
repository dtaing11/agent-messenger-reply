"""Order summary, confirmation tokens, submission and status transitions.

Guarantees enforced here, independent of the LLM:
- A confirmation token is bound to one customer, cart and cart version, and expires.
- Submission re-prices and re-validates inside one transaction; if anything material
  changed, the customer must confirm a fresh summary.
- The order row and its owner-SMS outbox rows are committed atomically.
- Re-submitting the same token (duplicate webhook / double tap) returns the same order.
- Status changes use conditional UPDATEs so concurrent owner actions cannot conflict.
"""

import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.agent.state_machine import InvalidTransition, OrderStatus, assert_transition
from app.config import Settings
from app.database.connection import transaction
from app.services import cart as cart_svc
from app.services import customers
from app.services.business import BusinessInfo, validate_pickup_time
from app.services.phone import display as phone_display
from app.services.pricing import money
from app.timeutil import from_db, local, to_db


class OrderError(Exception):
    """Customer-facing reason the order cannot proceed."""


class ConfirmationInvalid(OrderError):
    """Token unknown, expired, used for another cart version, or for another customer."""


class SummaryChanged(OrderError):
    """Price, availability or pickup validity changed since the summary was shown."""


# Owner's instruction: don't calculate tax, but always remind customers prices are before tax.
TAX_REMINDER = "Reminder: prices shown are before tax. Tax will be added at pickup."


LONG_NAME_NOTE = "Names longer than 7 letters have an extra charge. Our team will confirm the final price."


def _detail_lines(details: dict, indent: str) -> list[str]:
    out = []
    if details.get("message"):
        out.append(f'{indent}Message: "{details["message"]}"')
    if details.get("name"):
        out.append(f"{indent}Name: {details['name']}")
    if details.get("design"):
        out.append(f"{indent}Design: {details['design']}")
    for k, v in details.items():  # required choices, e.g. flavor
        if k not in ("message", "name", "design"):
            out.append(f"{indent}{k.replace('_', ' ').capitalize()}: {v}")
    return out


@dataclass(frozen=True)
class OrderSummary:
    token: str
    cart_version: int
    text: str
    total_cents: int


def missing_details(view: cart_svc.CartView, require_phone: bool = True) -> list[str]:
    missing = []
    if view.is_empty:
        missing.append("items")
    if view.pickup_time is None:
        missing.append("pickup time")
    if not view.customer_name:
        missing.append("pickup name")
    if require_phone and not view.customer_phone:
        missing.append("phone number")
    return missing


def _check_ready(view: cart_svc.CartView, biz: BusinessInfo, now: datetime, require_phone: bool = True) -> None:
    missing = missing_details(view, require_phone)
    if missing:
        raise OrderError("Still needed before I can summarize: " + ", ".join(missing) + ".")
    if view.unavailable:
        raise SummaryChanged("No longer available: " + ", ".join(view.unavailable) + ".")
    days, notice = view.advance_notice()
    pv = validate_pickup_time(biz, view.pickup_time, now, days, notice)
    if not pv.ok:
        raise SummaryChanged(pv.reason or "That pickup time is no longer available.")


def render_summary(view: cart_svc.CartView, biz: BusinessInfo) -> str:
    lines = ["Here is your order request:"]
    for l in view.lines:
        mods = f" ({', '.join(name for _, name in l.modifiers)})" if l.modifiers else ""
        lines.append(f"{l.quantity} x {l.name}{mods} — {money(l.line_total_cents)}")
        lines += _detail_lines(l.details, indent="   ")
        if l.surcharge_cents:
            lines.append(f"   Includes {money(l.surcharge_cents)} long-name surcharge")
    t = view.totals
    if t.tax_included:
        lines += [f"Subtotal: {money(t.subtotal_cents)}", f"Tax: {money(t.tax_cents)}", f"Total: {money(t.total_cents)}"]
    else:
        lines.append(f"Subtotal: {money(t.subtotal_cents)} before tax")
    pickup = local(view.pickup_time, biz.timezone)
    lines += [
        f"Pickup: {pickup:%a %b} {pickup.day} at {pickup:%-I:%M %p}",
        f"Name: {view.customer_name}",
        *([f"Phone: {phone_display(view.customer_phone)}"] if view.customer_phone else []),
        "",
        *biz.pickup_location_lines(),
        "Please make sure this is the store you'll pick up from.",
        "",
    ]
    if view.notes:
        lines.append(f"Notes: {view.notes}")
    lines.append("Payment: At pickup")
    if view.surcharge_pending:
        lines.append(LONG_NAME_NOTE)
    if view.needs_review:
        lines.append("Custom designs are reviewed by our team, who will confirm the details with you.")
    if not t.tax_included:
        lines.append(TAX_REMINDER)
    lines.append("Would you like to submit this order?")
    return "\n".join(lines)


def present_order_summary(
    conn: sqlite3.Connection, customer_id: int, biz: BusinessInfo, settings: Settings, now: datetime
) -> OrderSummary:
    """Validate the cart and issue a confirmation token bound to its current version."""
    view = cart_svc.view_cart(conn, customer_id, settings.tax_rate_bps)
    _check_ready(view, biz, now, settings.require_customer_phone)
    token = secrets.token_urlsafe(16)
    conn.execute(
        """INSERT INTO confirmation_tokens (token, cart_id, customer_id, cart_version, total_cents, expires_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (token, view.cart_id, customer_id, view.version, view.totals.total_cents,
         to_db(now + timedelta(minutes=settings.confirmation_token_ttl_minutes))),
    )
    # Showing a summary always means we are now waiting for the customer's explicit answer.
    conn.execute(
        "UPDATE conversations SET state = 'AWAITING_CONFIRMATION', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        " WHERE customer_id = ?",
        (customer_id,),
    )
    return OrderSummary(token, view.version, render_summary(view, biz), view.totals.total_cents)


def submit_order(
    conn: sqlite3.Connection, customer_id: int, token: str, biz: BusinessInfo, settings: Settings, now: datetime
) -> sqlite3.Row:
    """Commit an order for an explicitly confirmed summary. Idempotent per token."""
    with transaction(conn):
        tok = conn.execute("SELECT * FROM confirmation_tokens WHERE token = ?", (token,)).fetchone()
        if tok is None or tok["customer_id"] != customer_id:
            raise ConfirmationInvalid("That confirmation isn't valid. Let me show you the summary again.")

        if tok["used_at"]:
            existing = conn.execute(
                "SELECT * FROM orders WHERE cart_id = ? AND cart_version = ?", (tok["cart_id"], tok["cart_version"])
            ).fetchone()
            if existing:
                return existing  # duplicate confirmation: same order, no new SMS
            raise ConfirmationInvalid("That confirmation was already used.")

        if from_db(tok["expires_at"]) <= now:
            raise ConfirmationInvalid("That summary expired. Let me show you an updated one.")

        cart_row = conn.execute("SELECT * FROM carts WHERE id = ?", (tok["cart_id"],)).fetchone()
        if cart_row["status"] != "active" or cart_row["version"] != tok["cart_version"]:
            raise ConfirmationInvalid("Your order changed after that summary. Please review the updated one.")

        view = cart_svc.view_cart_by_id(conn, tok["cart_id"], settings.tax_rate_bps)
        _check_ready(view, biz, now, settings.require_customer_phone)
        if view.totals.total_cents != tok["total_cents"]:
            raise SummaryChanged("Prices changed since your summary. Please review the updated total.")

        needs_review = view.needs_review or view.totals.subtotal_cents >= settings.large_order_threshold_cents > 0
        expires_at = (
            to_db(now + timedelta(minutes=settings.pending_order_expiry_minutes))
            if settings.pending_order_expiry_minutes
            else None
        )
        cur = conn.execute(
            """INSERT INTO orders (customer_id, cart_id, cart_version, customer_name, customer_phone, location, pickup_time,
                 subtotal_cents, tax_cents, total_cents, status, notes, needs_review, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (customer_id, view.cart_id, view.version, view.customer_name, view.customer_phone, biz.location,
             to_db(view.pickup_time), view.totals.subtotal_cents, view.totals.tax_cents, view.totals.total_cents,
             str(OrderStatus.PENDING_ACCEPTANCE), view.notes, int(needs_review), expires_at),
        )
        customers.remember_contact(conn, customer_id, view.customer_name, view.customer_phone)
        order_id = cur.lastrowid
        for l in view.lines:
            conn.execute(
                """INSERT INTO order_items (order_id, item_id, item_name_snapshot, quantity, unit_price_cents,
                     modifiers_snapshot, line_total_cents, details_snapshot, surcharge_pending)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, l.item_id, l.name, l.quantity, l.unit_price_cents,
                 json.dumps([{"id": i, "name": n} for i, n in l.modifiers]), l.line_total_cents,
                 json.dumps(l.details, ensure_ascii=False), int(l.surcharge_pending)),
            )
        conn.execute(
            "INSERT INTO order_events (order_id, from_status, to_status, actor) VALUES (?, NULL, ?, 'customer')",
            (order_id, str(OrderStatus.PENDING_ACCEPTANCE)),
        )
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        body = format_owner_sms(conn, order, biz)
        for phone in settings.owner_phone_numbers:
            conn.execute(
                "INSERT INTO notifications (order_id, type, recipient, body, idempotency_key) VALUES (?, 'new_order', ?, ?, ?)",
                (order_id, phone, body, f"order:{order_id}:new_order:{phone}"),
            )
        conn.execute("UPDATE confirmation_tokens SET used_at = ? WHERE token = ?", (to_db(now), token))
        conn.execute(
            "UPDATE carts SET status = 'submitted', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
            (view.cart_id,),
        )
        conn.execute(
            "UPDATE conversations SET state = 'IDLE', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE customer_id = ?",
            (customer_id,),
        )
        return order


def confirmation_text(order: sqlite3.Row, biz: BusinessInfo) -> str:
    """Reply after the customer taps Confirm: repeats where to pick up."""
    pickup = local(from_db(order["pickup_time"]), biz.timezone)
    return "\n".join([
        f"Thank you! Order request #{order['id']} has been sent to our team for acceptance. "
        "I'll message you once they confirm.",
        "",
        f"Pickup: {pickup:%a %b} {pickup.day} at {pickup:%-I:%M %p}",
        *biz.pickup_location_lines(),
    ])


def format_owner_sms(conn: sqlite3.Connection, order: sqlite3.Row, biz: BusinessInfo) -> str:
    items = conn.execute(
        "SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order["id"],)
    ).fetchall()
    pickup = local(from_db(order["pickup_time"]), biz.timezone)
    history = customers.pickup_history(conn, order["customer_id"], order["customer_phone"], exclude_order_id=order["id"])
    lines = [
        f"NEW ORDER #{order['id']}",
        f"Customer: {order['customer_name']}",
        f"Phone: {phone_display(order['customer_phone']) or 'not given'}",
        customers.owner_history_line(history, biz),
        "Source: Facebook Messenger",
        f"Location: {order['location'].title()}",
        f"Pickup: {pickup:%b} {pickup.day}, {pickup:%-I:%M %p %Z}",
        "",
    ]
    for it in items:
        mods = json.loads(it["modifiers_snapshot"])
        suffix = f" ({', '.join(m['name'] for m in mods)})" if mods else ""
        lines.append(f"{it['quantity']} x {it['item_name_snapshot']}{suffix}")
        lines += _detail_lines(json.loads(it["details_snapshot"]), indent="  ")
        if it["surcharge_pending"]:
            lines.append("  LONG NAME: add surcharge and tell the customer")
    lines.append("")
    if order["tax_cents"]:
        lines.append(f"Total: {money(order['total_cents'])} (incl. tax {money(order['tax_cents'])})")
    else:
        lines.append(f"Subtotal: {money(order['subtotal_cents'])} (before tax)")
    lines += ["Payment: At pickup", "Status: Awaiting acceptance", f"Notes: {order['notes'] or 'None'}"]
    if order["needs_review"]:
        lines.append("REVIEW: custom design or large order, please check details.")
    lines += ["", f"Reply YES {order['id']} to accept or NO {order['id']} to decline."]
    return "\n".join(lines)


def transition_order(conn: sqlite3.Connection, order_id: int, target: OrderStatus, actor: str) -> sqlite3.Row:
    """Move an order to `target` if allowed. Conditional on the status we read, so two owners
    replying at once cannot both win."""
    with transaction(conn):
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order is None:
            raise OrderError(f"Order #{order_id} not found.")
        current = order["status"]
        assert_transition(current, target)
        cur = conn.execute(
            "UPDATE orders SET status = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ? AND status = ?",
            (str(target), order_id, current),
        )
        if cur.rowcount != 1:
            raise InvalidTransition(current, str(target))
        conn.execute(
            "INSERT INTO order_events (order_id, from_status, to_status, actor) VALUES (?, ?, ?, ?)",
            (order_id, current, str(target), actor),
        )
        return conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def cancel_by_customer(conn: sqlite3.Connection, customer_id: int, order_id: int, settings: Settings) -> sqlite3.Row:
    order = conn.execute("SELECT * FROM orders WHERE id = ? AND customer_id = ?", (order_id, customer_id)).fetchone()
    if order is None:
        raise OrderError("I couldn't find that order on your account.")
    allowed = {str(OrderStatus.PENDING_ACCEPTANCE)}
    if settings.customer_can_cancel_accepted:
        allowed.add(str(OrderStatus.ACCEPTED))
    if order["status"] not in allowed:
        raise OrderError("This order can't be cancelled here anymore. I'll connect you with the store.")
    return transition_order(conn, order_id, OrderStatus.CANCELLED, actor="customer")


def expire_pending_orders(conn: sqlite3.Connection, now: datetime) -> list[int]:
    """Mark PENDING_ACCEPTANCE orders past their expiry as EXPIRED. Returns expired ids."""
    rows = conn.execute(
        "SELECT id FROM orders WHERE status = ? AND expires_at IS NOT NULL AND expires_at <= ?",
        (str(OrderStatus.PENDING_ACCEPTANCE), to_db(now)),
    ).fetchall()
    expired = []
    for r in rows:
        try:
            transition_order(conn, r["id"], OrderStatus.EXPIRED, actor="system")
            expired.append(r["id"])
        except InvalidTransition:
            pass  # owner acted at the same moment; their action wins
    return expired

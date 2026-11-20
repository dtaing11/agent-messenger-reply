"""Customer profiles and owner-only pickup history.

The no-show record is for the owners only: it goes in owner texts and the portal, never to
the assistant or the customer.
"""

import sqlite3
from dataclasses import dataclass

from app.agent.state_machine import OrderStatus
from app.services.business import BusinessInfo
from app.services.phone import display
from app.timeutil import from_db, local


@dataclass(frozen=True)
class PickupHistory:
    orders: int
    picked_up: int
    no_shows: int
    last_no_show_id: int | None
    last_no_show_pickup: str | None  # UTC db timestamp


def pickup_history(conn: sqlite3.Connection, customer_id: int, exclude_order_id: int | None = None) -> PickupHistory:
    """Past orders by this Messenger account only. Not matched by phone number: anyone can
    type someone else's number, and a no-show must never land on the wrong person."""
    where, params = "customer_id = ?", [customer_id]
    if exclude_order_id is not None:
        where += " AND id != ?"
        params.append(exclude_order_id)
    row = conn.execute(
        f"""SELECT COUNT(*) AS n,
                   SUM(status = ?) AS picked,
                   SUM(status = ?) AS missed
            FROM orders WHERE {where}""",
        [str(OrderStatus.COMPLETED), str(OrderStatus.NO_SHOW), *params],
    ).fetchone()
    last = conn.execute(
        f"SELECT id, pickup_time FROM orders WHERE {where} AND status = ? ORDER BY pickup_time DESC LIMIT 1",
        [*params, str(OrderStatus.NO_SHOW)],
    ).fetchone()
    return PickupHistory(row["n"] or 0, row["picked"] or 0, row["missed"] or 0,
                         last["id"] if last else None, last["pickup_time"] if last else None)


def owner_history_line(h: PickupHistory, biz: BusinessInfo) -> str:
    if h.no_shows:
        when = local(from_db(h.last_no_show_pickup), biz.timezone)
        times = "1 past order" if h.no_shows == 1 else f"{h.no_shows} past orders"
        return (f"HEADS UP: this customer didn't pick up {times} "
                f"(last: #{h.last_no_show_id} on {when:%b} {when.day}). Picked up: {h.picked_up}.")
    if h.picked_up:
        return f"Returning customer: picked up {h.picked_up} past order(s), no missed pickups."
    if h.orders:
        return "Returning customer."
    return "First order from this customer."


def remember_contact(conn: sqlite3.Connection, customer_id: int, name: str | None, phone: str | None) -> None:
    conn.execute("UPDATE customers SET name = COALESCE(?, name), phone = COALESCE(?, phone) WHERE id = ?",
                 (name, phone, customer_id))


def saved_contact(conn: sqlite3.Connection, customer_id: int) -> dict | None:
    """Name and phone from the customer's last order, so the assistant can offer to reuse them."""
    row = conn.execute("SELECT name, phone FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if not row or not (row["name"] or row["phone"]):
        return None
    return {"name": row["name"], "phone": display(row["phone"]) or None}


def list_customers(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT c.id, c.name, c.phone, c.facebook_psid, c.created_at,
                  cv.bot_enabled, cv.handoff_reason, cv.last_customer_message_at,
                  COUNT(o.id) AS orders,
                  SUM(o.status = 'COMPLETED') AS picked_up,
                  SUM(o.status = 'NO_SHOW') AS no_shows,
                  MAX(o.created_at) AS last_order_at
           FROM customers c
           LEFT JOIN conversations cv ON cv.customer_id = c.id
           LEFT JOIN orders o ON o.customer_id = c.id
           GROUP BY c.id
           ORDER BY COALESCE(MAX(o.created_at), cv.last_customer_message_at, c.created_at) DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()

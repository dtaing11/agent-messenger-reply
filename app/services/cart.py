"""Cart operations. Every mutation bumps the cart version, which invalidates any
outstanding confirmation token bound to the old version."""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from app.database.connection import transaction
from app.services import custom_items, menu
from app.services.pricing import Totals, compute_totals
from app.timeutil import from_db, to_db


class CartError(Exception):
    """A customer-facing validation problem (unknown item, bad quantity, ...)."""


class DetailsNeeded(CartError):
    """A custom item is missing details; `questions` are what to ask the customer."""

    def __init__(self, item_name: str, missing: list[str], questions: list[str]):
        super().__init__(f"For the {item_name}: " + " ".join(questions))
        self.missing = missing
        self.questions = questions


@dataclass(frozen=True)
class CartLine:
    line_id: int
    item_id: str
    name: str
    unit_label: str
    quantity: int
    modifiers: tuple[tuple[str, str], ...]  # (id, name)
    unit_price_cents: int  # base price + modifier deltas + known surcharge, current menu price
    line_total_cents: int
    available: bool
    details: dict = field(default_factory=dict)  # custom items: message / name / design
    surcharge_cents: int = 0
    surcharge_pending: bool = False  # a surcharge applies but the store sets the amount
    needs_review: bool = False


@dataclass
class CartView:
    cart_id: int
    customer_id: int
    version: int
    lines: list[CartLine]
    totals: Totals
    pickup_time: datetime | None
    customer_name: str | None
    notes: str
    unavailable: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.lines

    @property
    def surcharge_pending(self) -> bool:
        return any(l.surcharge_pending for l in self.lines)

    @property
    def needs_review(self) -> bool:
        return any(l.needs_review for l in self.lines)


def get_or_create_cart(conn: sqlite3.Connection, customer_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM carts WHERE customer_id = ? AND status = 'active'", (customer_id,)).fetchone()
    if row:
        return row
    conn.execute("INSERT OR IGNORE INTO carts (customer_id) VALUES (?)", (customer_id,))
    return conn.execute("SELECT * FROM carts WHERE customer_id = ? AND status = 'active'", (customer_id,)).fetchone()


def _bump(conn: sqlite3.Connection, cart_id: int) -> None:
    conn.execute(
        "UPDATE carts SET version = version + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
        (cart_id,),
    )


def _normalize_modifiers(conn: sqlite3.Connection, item_id: str, modifier_ids: list[str] | None) -> str:
    ids = sorted(set(modifier_ids or []))
    if ids:
        valid = {m["id"] for m in menu.get_modifiers(conn, item_id)}
        bad = [m for m in ids if m not in valid]
        if bad:
            raise CartError(f"Modifier(s) not available for this item: {', '.join(bad)}")
    return json.dumps(ids)


def _normalize_details(item: sqlite3.Row, details: dict | None) -> str:
    rules = custom_items.CustomRules.from_json(item["custom_rules"])
    if rules is None:
        if details:
            raise CartError(f"{item['name']} doesn't take custom details.")
        return "{}"
    try:
        return custom_items.canonical(custom_items.normalize_details(rules, details))
    except custom_items.MissingDetails as exc:
        raise DetailsNeeded(item["name"], exc.missing, exc.questions) from exc
    except ValueError as exc:
        raise CartError(str(exc)) from exc


def _check_item(conn: sqlite3.Connection, item_id: str, quantity: int) -> sqlite3.Row:
    item = menu.get_item(conn, item_id)
    if item is None or not item["available"]:
        raise CartError(f"'{item_id}' is not on the current menu.")
    if quantity < 1 or quantity > item["max_quantity"]:
        raise CartError(f"Quantity for {item['name']} must be between 1 and {item['max_quantity']}.")
    return item


def add_item(
    conn: sqlite3.Connection,
    customer_id: int,
    item_id: str,
    quantity: int,
    modifiers: list[str] | None = None,
    details: dict | None = None,
) -> CartView:
    with transaction(conn):
        cart = get_or_create_cart(conn, customer_id)
        mods = _normalize_modifiers(conn, item_id, modifiers)
        item = _check_item(conn, item_id, quantity)
        det = _normalize_details(item, details)
        existing = conn.execute(
            "SELECT id, quantity FROM cart_items WHERE cart_id = ? AND item_id = ? AND modifiers = ? AND details = ?",
            (cart["id"], item_id, mods, det),
        ).fetchone()
        if existing:
            new_qty = existing["quantity"] + quantity
            _check_item(conn, item_id, new_qty)
            conn.execute("UPDATE cart_items SET quantity = ? WHERE id = ?", (new_qty, existing["id"]))
        else:
            conn.execute(
                "INSERT INTO cart_items (cart_id, item_id, quantity, modifiers, details) VALUES (?, ?, ?, ?, ?)",
                (cart["id"], item_id, quantity, mods, det),
            )
        _bump(conn, cart["id"])
    return view_cart(conn, customer_id)


def update_item(
    conn: sqlite3.Connection,
    customer_id: int,
    line_id: int,
    quantity: int,
    modifiers: list[str] | None = None,
    details: dict | None = None,
) -> CartView:
    """Set a line's quantity (0 removes it) and optionally replace its modifiers or details."""
    with transaction(conn):
        cart = get_or_create_cart(conn, customer_id)
        line = conn.execute("SELECT * FROM cart_items WHERE id = ? AND cart_id = ?", (line_id, cart["id"])).fetchone()
        if line is None:
            raise CartError("That item is not in your cart.")
        if quantity == 0:
            conn.execute("DELETE FROM cart_items WHERE id = ?", (line_id,))
        else:
            item = _check_item(conn, line["item_id"], quantity)
            mods = line["modifiers"] if modifiers is None else _normalize_modifiers(conn, line["item_id"], modifiers)
            det = line["details"] if details is None else _normalize_details(item, details)
            clash = conn.execute(
                "SELECT id FROM cart_items WHERE cart_id = ? AND item_id = ? AND modifiers = ? AND details = ? AND id != ?",
                (cart["id"], line["item_id"], mods, det, line_id),
            ).fetchone()
            if clash:
                raise CartError("You already have that item with those options; update that line instead.")
            conn.execute(
                "UPDATE cart_items SET quantity = ?, modifiers = ?, details = ? WHERE id = ?", (quantity, mods, det, line_id)
            )
        _bump(conn, cart["id"])
    return view_cart(conn, customer_id)


def remove_item(conn: sqlite3.Connection, customer_id: int, line_id: int) -> CartView:
    return update_item(conn, customer_id, line_id, 0)


def clear_cart(conn: sqlite3.Connection, customer_id: int) -> CartView:
    with transaction(conn):
        cart = get_or_create_cart(conn, customer_id)
        conn.execute("DELETE FROM cart_items WHERE cart_id = ?", (cart["id"],))
        _bump(conn, cart["id"])
    return view_cart(conn, customer_id)


def set_pickup_details(
    conn: sqlite3.Connection,
    customer_id: int,
    *,
    pickup_time: datetime | None = None,
    customer_name: str | None = None,
    notes: str | None = None,
) -> CartView:
    """Store already-validated pickup details. Callers validate pickup_time first."""
    if customer_name is not None:
        customer_name = customer_name.strip()
        if not (1 <= len(customer_name) <= 80):
            raise CartError("Please give a pickup name between 1 and 80 characters.")
    if notes is not None and len(notes) > 500:
        raise CartError("Notes must be 500 characters or fewer.")
    with transaction(conn):
        cart = get_or_create_cart(conn, customer_id)
        conn.execute(
            """UPDATE carts SET
                 pickup_time = COALESCE(?, pickup_time),
                 customer_name = COALESCE(?, customer_name),
                 notes = COALESCE(?, notes)
               WHERE id = ?""",
            (to_db(pickup_time) if pickup_time else None, customer_name, notes, cart["id"]),
        )
        _bump(conn, cart["id"])
    return view_cart(conn, customer_id)


def view_cart(conn: sqlite3.Connection, customer_id: int, tax_rate_bps: int = 0) -> CartView:
    """Read the active cart priced at *current* menu prices."""
    cart = get_or_create_cart(conn, customer_id)
    return _view(conn, cart, tax_rate_bps)


def view_cart_by_id(conn: sqlite3.Connection, cart_id: int, tax_rate_bps: int = 0) -> CartView:
    cart = conn.execute("SELECT * FROM carts WHERE id = ?", (cart_id,)).fetchone()
    return _view(conn, cart, tax_rate_bps)


def _view(conn: sqlite3.Connection, cart: sqlite3.Row, tax_rate_bps: int) -> CartView:
    rows = conn.execute(
        """SELECT ci.id AS line_id, ci.item_id, ci.quantity, ci.modifiers, ci.details,
                  mi.name, mi.unit_label, mi.price_cents, mi.available, mi.custom_rules
           FROM cart_items ci JOIN menu_items mi ON mi.id = ci.item_id
           WHERE ci.cart_id = ? ORDER BY ci.id""",
        (cart["id"],),
    ).fetchall()
    lines, unavailable = [], []
    for r in rows:
        mod_ids = json.loads(r["modifiers"])
        mods, unit, ok = [], r["price_cents"], bool(r["available"])
        for mid in mod_ids:
            m = conn.execute("SELECT * FROM menu_modifiers WHERE id = ?", (mid,)).fetchone()
            if m is None or not m["available"]:
                ok = False
                mods.append((mid, m["name"] if m else mid))
                continue
            mods.append((m["id"], m["name"]))
            unit += m["price_delta_cents"]
        details = json.loads(r["details"])
        rules = custom_items.CustomRules.from_json(r["custom_rules"])
        extra, pending = custom_items.surcharge(rules, details) if rules else (0, False)
        unit += extra
        if not ok:
            unavailable.append(r["name"])
        lines.append(
            CartLine(
                r["line_id"], r["item_id"], r["name"], r["unit_label"], r["quantity"], tuple(mods), unit,
                unit * r["quantity"], ok, details=details, surcharge_cents=extra, surcharge_pending=pending,
                needs_review=bool(rules and rules.always_review),
            )
        )
    return CartView(
        cart_id=cart["id"],
        customer_id=cart["customer_id"],
        version=cart["version"],
        lines=lines,
        totals=compute_totals([l.line_total_cents for l in lines], tax_rate_bps),
        pickup_time=from_db(cart["pickup_time"]) if cart["pickup_time"] else None,
        customer_name=cart["customer_name"],
        notes=cart["notes"],
        unavailable=unavailable,
    )

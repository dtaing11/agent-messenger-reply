"""Cart operations. Every mutation bumps the cart version, which invalidates any
outstanding confirmation token bound to the old version."""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from app.database.connection import transaction
from app.services import custom_items, menu, phone
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
    min_days_ahead: int = 0  # calendar days of notice this item needs (Special Design Donut: 1)


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
    customer_phone: str | None = None  # E.164

    @property
    def is_empty(self) -> bool:
        return not self.lines

    @property
    def surcharge_pending(self) -> bool:
        return any(l.surcharge_pending for l in self.lines)

    @property
    def needs_review(self) -> bool:
        return any(l.needs_review for l in self.lines)

    def advance_notice(self) -> tuple[int, str | None]:
        """(days, what to tell the customer) for the item in the cart needing the most notice."""
        line = max(self.lines, key=lambda l: l.min_days_ahead, default=None)
        if line is None or not line.min_days_ahead:
            return 0, None
        return line.min_days_ahead, custom_items.notice_text(line.name, line.min_days_ahead)


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


def _pool_selection(conn: sqlite3.Connection, item: sqlite3.Row, c: custom_items.Choice, details: dict) -> str:
    """A mix like '2 Twist (Chocolate), 2 Cinnamon Roll, 2 Pinecone' for a pick-any item."""
    names = [r["name"] for r in menu.pool_items(conn, c.pool_category, c.pool_price_cents)]
    labels = menu.pool_labels(conn, c.pool_category, c.pool_price_cents)
    raw = " ".join(str(details.get(c.key) or details.get(c.label) or details.get("selection") or "").split())
    question = (f"Which {c.count} {c.label} would you like? Pick any mix of: " + "; ".join(labels) + "."
                if labels else f"Which {c.label} would you like?")
    low = raw.lower()
    if not raw or not (any(n.lower() in low for n in names) or any(w in low for w in custom_items.ANY_MIX)):
        raise custom_items.MissingDetails([c.key], [question])
    if len(raw) > 200:
        raise CartError("Please keep the donut selection short, like: 2 Twist (Chocolate), 2 Cinnamon Roll, 2 Pinecone.")
    pool = [(r["name"], [o for ch in custom_items.parse_choices(r["choices"]) if not ch.is_pool for o in ch.options])
            for r in menu.pool_items(conn, c.pool_category, c.pool_price_cents)]
    others = [r["name"] for r in conn.execute("SELECT name FROM menu_items WHERE archived = 0") if r["name"] not in names]
    try:
        return custom_items.check_pool_mix(raw, c, pool, others)
    except custom_items.MixError as exc:
        raise CartError(str(exc)) from exc


def _normalize_details(conn: sqlite3.Connection, item: sqlite3.Row, details: dict | None) -> str:
    rules = custom_items.CustomRules.from_json(item["custom_rules"])
    choices = custom_items.parse_choices(item["choices"])
    details = {k: v for k, v in (details or {}).items() if v not in (None, "")}
    out: dict = {}
    try:
        if choices:
            out.update(custom_items.apply_choices(choices, item["name"], details))
            for c in (c for c in choices if c.is_pool):
                out[c.key] = _pool_selection(conn, item, c, details)
            taken = {c.key for c in choices} | {c.label for c in choices} | {c.label.lower() for c in choices}
            details = {k: v for k, v in details.items() if k not in taken}
        if rules is not None:
            out.update(custom_items.normalize_details(rules, details))
        elif details:
            allowed = ", ".join(c.key for c in choices) or "none"
            raise CartError(f"{item['name']} doesn't take those details (allowed: {allowed}).")
    except custom_items.MissingDetails as exc:
        raise DetailsNeeded(item["name"], exc.missing, exc.questions) from exc
    except CartError:
        raise
    except ValueError as exc:
        raise CartError(str(exc)) from exc
    return custom_items.canonical(out) if out else "{}"


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
        det = _normalize_details(conn, item, details)
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
            det = line["details"] if details is None else _normalize_details(conn, item, details)
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


def one_line(text: str) -> str:
    """Customer text that goes into an owner alert: one line, no control characters."""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in str(text)).split())


def set_pickup_details(
    conn: sqlite3.Connection,
    customer_id: int,
    *,
    pickup_time: datetime | None = None,
    customer_name: str | None = None,
    notes: str | None = None,
    customer_phone: str | None = None,
) -> CartView:
    """Store already-validated pickup details. Callers validate pickup_time first.
    customer_phone may be typed any common way; it's stored as E.164."""
    if customer_phone is not None:
        try:
            customer_phone = phone.normalize_us(customer_phone)
        except phone.PhoneError as exc:
            raise CartError(str(exc)) from exc
    if customer_name is not None:
        customer_name = one_line(customer_name)  # a newline could fake extra lines in the owner alert
        if not (1 <= len(customer_name) <= 80):
            raise CartError("Please give a pickup name between 1 and 80 characters.")
    if notes is not None:
        notes = one_line(notes)
        if len(notes) > 500:
            raise CartError("Notes must be 500 characters or fewer.")
    with transaction(conn):
        cart = get_or_create_cart(conn, customer_id)
        conn.execute(
            """UPDATE carts SET
                 pickup_time = COALESCE(?, pickup_time),
                 customer_name = COALESCE(?, customer_name),
                 customer_phone = COALESCE(?, customer_phone),
                 notes = COALESCE(?, notes)
               WHERE id = ?""",
            (to_db(pickup_time) if pickup_time else None, customer_name, customer_phone, notes, cart["id"]),
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
                min_days_ahead=rules.min_days_ahead if rules else 0,
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
        customer_phone=cart["customer_phone"],
    )

"""Menu editing from the portal. Changes are live: the bot reads prices and items from SQL
on every message, and a customer's open summary is re-checked at Confirm, so a price change
can never slip into an order without the customer seeing it.

Once the menu has been edited here, startup no longer re-imports data/menu.json (that would
undo the edits). The portal has a button to re-import it on purpose.
"""

import json
import re
import sqlite3
from decimal import Decimal, InvalidOperation

from app.database.connection import transaction
from app.services import custom_items
from app.settings.runtime import get_marker, set_marker
from app.timeutil import to_db, utcnow

EDITED_MARKER = "menu.edited_in_portal"
MAX_PRICE_CENTS = 100_000  # $1,000


class MenuError(ValueError):
    """A customer-safe message about why a menu change was refused."""


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:60] or "item"


def default_category_name(cat_id: str) -> str:
    return cat_id.replace("_", " ").title()


def parse_price(value: str | None) -> int:
    text = str(value or "").strip().lstrip("$").replace(",", "")
    try:
        d = Decimal(text)
    except InvalidOperation:
        raise MenuError("Enter the price as a number, like 12.99.")
    if d < 0 or d.as_tuple().exponent < -2:
        raise MenuError("Prices need 0 or more dollars and at most 2 decimals, like 12.99.")
    cents = int(d * 100)
    if cents > MAX_PRICE_CENTS:
        raise MenuError("That price looks too high. Prices are per item, before tax.")
    return cents


def _clean_name(value: str | None, what: str = "Name") -> str:
    name = " ".join(str(value or "").split())
    if not name:
        raise MenuError(f"{what} can't be empty.")
    if len(name) > 80:
        raise MenuError(f"{what} must be 80 characters or fewer.")
    return name


def mark_edited(conn: sqlite3.Connection) -> None:
    set_marker(conn, EDITED_MARKER, to_db(utcnow()))


def edited_in_portal(conn: sqlite3.Connection) -> bool:
    return get_marker(conn, EDITED_MARKER) is not None


def ensure_categories(conn: sqlite3.Connection, named: dict[str, str] | None = None) -> None:
    """Every item's category gets a row. Categories listed in the menu file (`named`, in
    order) take the file's names and order; this only runs on a file import."""
    named = named or {}
    for pos, (cat, name) in enumerate(named.items()):
        conn.execute(
            """INSERT INTO menu_categories (id, name, sort_order) VALUES (?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name = excluded.name, sort_order = excluded.sort_order""",
            (cat, name or default_category_name(cat), (pos + 1) * 10),
        )
    for (cat,) in conn.execute("SELECT DISTINCT category FROM menu_items ORDER BY category").fetchall():
        nxt = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 10 FROM menu_categories").fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO menu_categories (id, name, sort_order) VALUES (?, ?, ?)",
                     (cat, default_category_name(cat), nxt))


# ---------------------------------------------------------------- reading

def categories(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM menu_categories ORDER BY sort_order, name").fetchall()


def menu_tree(conn: sqlite3.Connection) -> list[dict]:
    """Categories in order, each with its (non-removed) items and their add-ons."""
    mods: dict[str, list[sqlite3.Row]] = {}
    for m in conn.execute("SELECT * FROM menu_modifiers ORDER BY id"):
        mods.setdefault(m["item_id"], []).append(m)
    items: dict[str, list[dict]] = {}
    for it in conn.execute("SELECT * FROM menu_items WHERE archived = 0 ORDER BY sort_order, name"):
        items.setdefault(it["category"], []).append({"row": it, "modifiers": mods.get(it["id"], [])})
    return [{"cat": c, "items": items.get(c["id"], [])} for c in categories(conn)]


def export_menu(conn: sqlite3.Connection) -> dict:
    """The current menu in data/menu.json format (for backup or committing to the repo)."""
    out = []
    for group in menu_tree(conn):
        for entry in group["items"]:
            it = entry["row"]
            item = {"id": it["id"], "name": it["name"], "category": it["category"], "unit_label": it["unit_label"],
                    "price_cents": it["price_cents"], "max_quantity": it["max_quantity"], "description": it["description"],
                    "search_terms": it["search_terms"]}
            if not it["available"]:
                item["available"] = False
            if it["custom_rules"]:
                item["custom"] = json.loads(it["custom_rules"])
            if it["choices"]:
                item["choices"] = json.loads(it["choices"])
            if entry["modifiers"]:
                item["modifiers"] = [{"id": m["id"], "name": m["name"], "price_delta_cents": m["price_delta_cents"],
                                      **({} if m["available"] else {"available": False})} for m in entry["modifiers"]]
            out.append(item)
    cats = [{"id": c["id"], "name": c["name"]} for c in categories(conn)]
    return {"_comment": "Exported from the developer portal.", "categories": cats, "items": out}


# ---------------------------------------------------------------- categories

def add_category(conn: sqlite3.Connection, name: str) -> str:
    name = _clean_name(name, "Category name")
    base = slugify(name)
    cat_id, n = base, 2
    while conn.execute("SELECT 1 FROM menu_categories WHERE id = ?", (cat_id,)).fetchone():
        if conn.execute("SELECT 1 FROM menu_categories WHERE id = ? AND lower(name) = lower(?)", (cat_id, name)).fetchone():
            raise MenuError(f"There's already a category called {name}.")
        cat_id, n = f"{base}_{n}", n + 1
    nxt = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 10 FROM menu_categories").fetchone()[0]
    conn.execute("INSERT INTO menu_categories (id, name, sort_order) VALUES (?, ?, ?)", (cat_id, name, nxt))
    mark_edited(conn)
    return cat_id


def rename_category(conn: sqlite3.Connection, cat_id: str, name: str) -> None:
    name = _clean_name(name, "Category name")
    if conn.execute("UPDATE menu_categories SET name = ? WHERE id = ?", (name, cat_id)).rowcount != 1:
        raise MenuError("That category no longer exists. Reload the page.")
    mark_edited(conn)


def move_category(conn: sqlite3.Connection, cat_id: str, direction: int) -> None:
    cats = [c["id"] for c in categories(conn)]
    if cat_id not in cats:
        raise MenuError("That category no longer exists. Reload the page.")
    i = cats.index(cat_id)
    j = max(0, min(len(cats) - 1, i + direction))
    cats[i], cats[j] = cats[j], cats[i]
    with transaction(conn):
        for pos, cid in enumerate(cats):
            conn.execute("UPDATE menu_categories SET sort_order = ? WHERE id = ?", ((pos + 1) * 10, cid))
    mark_edited(conn)


def delete_category(conn: sqlite3.Connection, cat_id: str) -> None:
    if conn.execute("SELECT 1 FROM menu_items WHERE category = ? AND archived = 0", (cat_id,)).fetchone():
        raise MenuError("Move or remove the items in this category first.")
    if conn.execute("SELECT 1 FROM menu_items WHERE category = ?", (cat_id,)).fetchone():
        raise MenuError("Removed items from old orders still use this category, so it stays. Rename it instead.")
    conn.execute("DELETE FROM menu_categories WHERE id = ?", (cat_id,))
    mark_edited(conn)


# ---------------------------------------------------------------- items

def _category_exists(conn: sqlite3.Connection, cat_id: str) -> None:
    if not conn.execute("SELECT 1 FROM menu_categories WHERE id = ?", (cat_id,)).fetchone():
        raise MenuError("Pick a category.")


def add_item(conn: sqlite3.Connection, category: str, name: str, price: str, unit_label: str = "each") -> str:
    name = _clean_name(name)
    _category_exists(conn, category)
    cents = parse_price(price)
    if conn.execute("SELECT 1 FROM menu_items WHERE lower(name) = lower(?) AND archived = 0", (name,)).fetchone():
        raise MenuError(f"There's already an item called {name}.")
    base = slugify(name)
    item_id, n = base, 2
    while conn.execute("SELECT 1 FROM menu_items WHERE id = ?", (item_id,)).fetchone():
        item_id, n = f"{base}_{n}", n + 1
    nxt = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 10 FROM menu_items WHERE category = ?", (category,)).fetchone()[0]
    conn.execute(
        "INSERT INTO menu_items (id, name, category, unit_label, price_cents, sort_order) VALUES (?, ?, ?, ?, ?, ?)",
        (item_id, name, category, (unit_label or "each").strip()[:30] or "each", cents, nxt),
    )
    mark_edited(conn)
    return item_id


def update_item(conn: sqlite3.Connection, item_id: str, form: dict) -> sqlite3.Row:
    """Save the editable fields of one item (and its add-ons) from a portal form."""
    cur = conn.execute("SELECT * FROM menu_items WHERE id = ? AND archived = 0", (item_id,)).fetchone()
    if cur is None:
        raise MenuError("That item no longer exists. Reload the page.")
    name = _clean_name(form.get("name", cur["name"]))
    category = str(form.get("category", cur["category"]))
    _category_exists(conn, category)
    cents = parse_price(form.get("price", cur["price_cents"] / 100))
    try:
        max_q = int(form.get("max_quantity", cur["max_quantity"]))
    except (TypeError, ValueError):
        raise MenuError("Max per order must be a whole number.")
    if not 1 <= max_q <= 1000:
        raise MenuError("Max per order must be between 1 and 1000.")
    unit = str(form.get("unit_label", cur["unit_label"])).strip()[:30] or "each"
    desc = " ".join(str(form.get("description", cur["description"])).split())[:300]
    terms = " ".join(str(form.get("search_terms", cur["search_terms"])).split())[:300]
    available = 1 if form.get("available") in ("on", "1", "true", True) else 0
    dup = conn.execute("SELECT 1 FROM menu_items WHERE lower(name) = lower(?) AND id != ? AND archived = 0",
                       (name, item_id)).fetchone()
    if dup:
        raise MenuError(f"There's already an item called {name}.")

    mod_updates = []
    for m in conn.execute("SELECT * FROM menu_modifiers WHERE item_id = ?", (item_id,)).fetchall():
        key = f"mod_{m['id']}"
        if f"{key}_name" not in form:
            continue
        mod_updates.append((_clean_name(form.get(f"{key}_name"), "Add-on name"), parse_price(form.get(f"{key}_price", "0")),
                            1 if form.get(f"{key}_available") == "on" else 0, m["id"]))
    choices = None
    if "choice_options" in form:
        opts = [o.strip() for o in str(form.get("choice_options", "")).replace("\n", ",").split(",") if o.strip()]
        label = " ".join(str(form.get("choice_label", "")).split()) or "Flavor"
        if len(opts) == 1:
            raise MenuError("A choice needs at least 2 options, separated by commas.")
        if any(len(o) > 60 for o in opts) or len(opts) > 20:
            raise MenuError("Keep choices to 20 options of 60 characters or fewer.")
        choices = custom_items.choices_json([custom_items.Choice(
            key=label.lower().replace(" ", "_")[:30], label=label[:30], options=tuple(dict.fromkeys(opts)))]) if opts else ""
    new_mod = None
    if str(form.get("new_mod_name", "")).strip():
        new_mod = (_clean_name(form.get("new_mod_name"), "Add-on name"), parse_price(form.get("new_mod_price", "0")))

    with transaction(conn):
        conn.execute(
            """UPDATE menu_items SET name = ?, category = ?, price_cents = ?, max_quantity = ?, unit_label = ?,
                 description = ?, search_terms = ?, available = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
               WHERE id = ?""",
            (name, category, cents, max_q, unit, desc, terms, available, item_id),
        )
        if choices is not None:
            conn.execute("UPDATE menu_items SET choices = ? WHERE id = ?", (choices or None, item_id))
        for mu in mod_updates:
            conn.execute("UPDATE menu_modifiers SET name = ?, price_delta_cents = ?, available = ? WHERE id = ?", mu)
        if new_mod:
            base = f"{item_id}__{slugify(new_mod[0])}"
            mod_id, n = base, 2
            while conn.execute("SELECT 1 FROM menu_modifiers WHERE id = ?", (mod_id,)).fetchone():
                mod_id, n = f"{base}_{n}", n + 1
            conn.execute("INSERT INTO menu_modifiers (id, item_id, name, price_delta_cents) VALUES (?, ?, ?, ?)",
                         (mod_id, item_id, *new_mod))
    mark_edited(conn)
    return conn.execute("SELECT * FROM menu_items WHERE id = ?", (item_id,)).fetchone()


def set_available(conn: sqlite3.Connection, item_id: str, available: bool) -> None:
    if conn.execute("UPDATE menu_items SET available = ? WHERE id = ? AND archived = 0",
                    (1 if available else 0, item_id)).rowcount != 1:
        raise MenuError("That item no longer exists. Reload the page.")
    mark_edited(conn)


def remove_item(conn: sqlite3.Connection, item_id: str) -> str:
    """Delete the item, or archive it if a cart still points at it. Past orders keep their
    own copy of names and prices either way."""
    row = conn.execute("SELECT name FROM menu_items WHERE id = ? AND archived = 0", (item_id,)).fetchone()
    if row is None:
        raise MenuError("That item no longer exists. Reload the page.")
    if conn.execute("SELECT 1 FROM cart_items WHERE item_id = ? LIMIT 1", (item_id,)).fetchone():
        conn.execute("UPDATE menu_items SET available = 0, archived = 1 WHERE id = ?", (item_id,))
    else:
        conn.execute("DELETE FROM menu_items WHERE id = ?", (item_id,))
    mark_edited(conn)
    return row["name"]

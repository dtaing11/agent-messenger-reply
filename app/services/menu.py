"""Menu: SQL is the source of truth for items, prices and availability."""

import json
import re
import sqlite3
from pathlib import Path

from app.database.connection import transaction


def seed_menu(conn: sqlite3.Connection, path: Path) -> int:
    """Upsert the menu file. Items/modifiers missing from the file are marked unavailable
    (never deleted) so historical carts and orders keep their references."""
    from app.services.menu_admin import ensure_categories

    data = json.loads(Path(path).read_text())
    items = data["items"]
    with transaction(conn):
        conn.execute("UPDATE menu_items SET available = 0")
        conn.execute("UPDATE menu_modifiers SET available = 0")
        for it in items:
            conn.execute(
                """INSERT INTO menu_items (id, name, category, description, search_terms, unit_label, price_cents, max_quantity, available, custom_rules, choices)
                   VALUES (:id, :name, :category, :description, :search_terms, :unit_label, :price_cents, :max_quantity, :available, :custom_rules, :choices)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, category=excluded.category,
                     description=excluded.description, search_terms=excluded.search_terms, unit_label=excluded.unit_label,
                     price_cents=excluded.price_cents, max_quantity=excluded.max_quantity,
                     available=excluded.available, custom_rules=excluded.custom_rules, choices=excluded.choices, archived=0,
                     updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
                {
                    "id": it["id"],
                    "name": it["name"],
                    "category": it["category"],
                    "description": it.get("description", ""),
                    "search_terms": it.get("search_terms", ""),
                    "unit_label": it.get("unit_label", "each"),
                    "price_cents": int(it["price_cents"]),
                    "max_quantity": int(it.get("max_quantity", 100)),
                    "available": 1 if it.get("available", True) else 0,
                    "custom_rules": json.dumps(it["custom"]) if it.get("custom") else None,
                    "choices": json.dumps(it["choices"], ensure_ascii=False) if it.get("choices") else None,
                },
            )
            for m in it.get("modifiers", []):
                conn.execute(
                    """INSERT INTO menu_modifiers (id, item_id, name, price_delta_cents, available)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET item_id=excluded.item_id, name=excluded.name,
                         price_delta_cents=excluded.price_delta_cents, available=excluded.available""",
                    (m["id"], it["id"], m["name"], int(m.get("price_delta_cents", 0)), 1 if m.get("available", True) else 0),
                )
        ensure_categories(conn, {c["id"]: c["name"] for c in data.get("categories", [])})
    return len(items)


def get_item(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM menu_items WHERE id = ?", (item_id,)).fetchone()


def get_modifiers(conn: sqlite3.Connection, item_id: str, only_available: bool = True) -> list[sqlite3.Row]:
    sql = "SELECT * FROM menu_modifiers WHERE item_id = ?" + (" AND available = 1" if only_available else "")
    return conn.execute(sql + " ORDER BY id", (item_id,)).fetchall()


_ITEMS = """SELECT mi.*, COALESCE(mc.name, mi.category) AS category_name
            FROM menu_items mi LEFT JOIN menu_categories mc ON mc.id = mi.category
            WHERE mi.available = 1 AND mi.archived = 0"""


_SEARCH_STOPWORDS = frozenset(
    "a an the and or of for to in on at with without some something any anything one ones thing kind sort "
    "i me my we you your do does have has got get can could would like want need please give show "
    "is are was it its that this what which who how much many".split()
)


def query_terms(query: str) -> list[str]:
    """The words worth matching: lowercased, no filler words, simple plurals trimmed
    ("donuts" -> "donut", so it also matches "Donut")."""
    words = re.findall(r"[a-z0-9\u00c0-\u024f']+", (query or "").lower())
    out = []
    for w in words:
        w = w.strip("'")
        if len(w) < 2 or w in _SEARCH_STOPWORDS:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if w not in out:
            out.append(w)
    return out[:8]


_SEARCH_FIELDS = ("name", "category", "category_name", "description", "search_terms")


def _term_pattern(term: str) -> re.Pattern:
    """Whole words, so "ice" doesn't find "Iced Coffee" or "Juice". A plural counts ("donut"
    finds "Donuts"), and a longer word may be the start of one ("choco", "glaze")."""
    t = re.escape(term)
    return re.compile(rf"\b{t}(?:s|es)?\b" if len(term) < 5 else rf"\b{t}", re.I)


def _matched(row: sqlite3.Row, patterns: list[re.Pattern]) -> tuple[int, int]:
    """(how many of the words the item matches, how many of them are in its name)."""
    keys = row.keys()
    text = " ".join(str(row[f] or "") for f in _SEARCH_FIELDS if f in keys)
    return sum(1 for p in patterns if p.search(text)), sum(1 for p in patterns if p.search(row["name"] or ""))


def matches_all(row: sqlite3.Row, query: str) -> bool:
    """Whether the item matches every word worth matching in the query."""
    patterns = [_term_pattern(t) for t in query_terms(query)]
    return bool(patterns) and _matched(row, patterns)[0] == len(patterns)


def search_menu(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[sqlite3.Row]:
    """Keyword match over available items (name, category, description, search words), whole
    words only. Items matching more of the words come first, and a word in the name counts
    double, so "chocolate donut" ranks Chocolate items first."""
    terms = query_terms(query)
    if not terms:
        return conn.execute(f"{_ITEMS} ORDER BY mc.sort_order, mi.sort_order, mi.name LIMIT ?", (limit,)).fetchall()
    patterns = [_term_pattern(t) for t in terms]
    # LIKE narrows the rows; the word patterns decide.
    match = " OR ".join(f"lower(COALESCE({f}, '')) LIKE ?" for f in _SEARCH_FIELDS)
    where = " OR ".join([f"({match})"] * len(terms))
    rows = conn.execute(f"SELECT * FROM ({_ITEMS}) WHERE {where}",
                        [f"%{t}%" for t in terms for _ in _SEARCH_FIELDS]).fetchall()
    scored = [(hits + in_name, r) for r in rows for hits, in_name in [_matched(r, patterns)] if hits]
    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    return [r for _, r in scored[:limit]]


def pool_items(conn: sqlite3.Connection, category: str, price_cents: int) -> list[sqlite3.Row]:
    """Available items in a category at a price, e.g. every $2.09 donut (the "Big Donuts")."""
    return conn.execute(
        "SELECT * FROM menu_items WHERE category = ? AND price_cents = ? AND available = 1 AND archived = 0 ORDER BY sort_order, name",
        (category, price_cents),
    ).fetchall()


def pool_labels(conn: sqlite3.Connection, category: str, price_cents: int) -> list[str]:
    """'Twist (Cinnamon Glazed, Cinnamon Sugar, Chocolate or Glazed)' style labels for a pool."""
    from app.services.custom_items import parse_choices

    out = []
    for it in pool_items(conn, category, price_cents):
        flavors = [o for c in parse_choices(it["choices"]) if not c.is_pool for o in c.options]
        if flavors:
            listed = ", ".join(flavors[:-1]) + f" or {flavors[-1]}" if len(flavors) > 1 else flavors[0]
            out.append(f"{it['name']} ({listed})")
        else:
            out.append(it["name"])
    return out


def browse(conn: sqlite3.Connection, category: str | None = None) -> dict:
    """A compact, scannable view of the menu for the assistant: the categories, or one
    category's items with each item's own price (so a reply never claims "all $X" wrongly)."""
    from app.services.pricing import money

    rows = conn.execute(
        f"SELECT * FROM ({_ITEMS}) ORDER BY (SELECT sort_order FROM menu_categories WHERE id = category), sort_order, name"
    ).fetchall()
    by_cat: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)

    def price_range(items: list[sqlite3.Row]) -> str:
        lo, hi = min(i["price_cents"] for i in items), max(i["price_cents"] for i in items)
        return money(lo) if lo == hi else f"{money(lo)}-{money(hi)}"

    if not category:
        return {"categories": [{"category": items[0]["category_name"], "items": len(items), "prices": price_range(items)}
                               for items in by_cat.values()]}
    want = " ".join(category.lower().replace("_", " ").split())
    match = [c for c, items in by_cat.items()
             if want in (c.replace("_", " "), items[0]["category_name"].lower())]
    match = match or [c for c, items in by_cat.items()
                      if want in c.replace("_", " ") or want in items[0]["category_name"].lower()]
    if not match:
        return {"error": f"No category called {category!r}.",
                "categories": [items[0]["category_name"] for items in by_cat.values()]}
    items = by_cat[match[0]]
    prices = {i["price_cents"] for i in items}
    return {
        "category": items[0]["category_name"],
        "items": [{"item_id": i["id"], "name": i["name"], "price_before_tax": money(i["price_cents"]),
                   "has_choices": bool(i["choices"] and i["choices"] != "[]") or bool(i["custom_rules"])} for i in items],
        "same_price": len(prices) == 1,
        "price_note": (f"Every item here is {money(next(iter(prices)))}." if len(prices) == 1
                       else "Prices differ: give each item its own price."),
    }

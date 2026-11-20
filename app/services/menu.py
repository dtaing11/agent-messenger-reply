"""Menu: SQL is the source of truth for items, prices and availability."""

import json
import sqlite3
from pathlib import Path

from app.database.connection import transaction


def seed_menu(conn: sqlite3.Connection, path: Path) -> int:
    """Upsert the menu file. Items/modifiers missing from the file are marked unavailable
    (never deleted) so historical carts and orders keep their references."""
    data = json.loads(Path(path).read_text())
    items = data["items"]
    with transaction(conn):
        conn.execute("UPDATE menu_items SET available = 0")
        conn.execute("UPDATE menu_modifiers SET available = 0")
        for it in items:
            conn.execute(
                """INSERT INTO menu_items (id, name, category, description, unit_label, price_cents, max_quantity, available, custom_rules)
                   VALUES (:id, :name, :category, :description, :unit_label, :price_cents, :max_quantity, :available, :custom_rules)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name, category=excluded.category,
                     description=excluded.description, unit_label=excluded.unit_label,
                     price_cents=excluded.price_cents, max_quantity=excluded.max_quantity,
                     available=excluded.available, custom_rules=excluded.custom_rules,
                     updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
                {
                    "id": it["id"],
                    "name": it["name"],
                    "category": it["category"],
                    "description": it.get("description", ""),
                    "unit_label": it.get("unit_label", "each"),
                    "price_cents": int(it["price_cents"]),
                    "max_quantity": int(it.get("max_quantity", 100)),
                    "available": 1 if it.get("available", True) else 0,
                    "custom_rules": json.dumps(it["custom"]) if it.get("custom") else None,
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
    return len(items)


def get_item(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM menu_items WHERE id = ?", (item_id,)).fetchone()


def get_modifiers(conn: sqlite3.Connection, item_id: str, only_available: bool = True) -> list[sqlite3.Row]:
    sql = "SELECT * FROM menu_modifiers WHERE item_id = ?" + (" AND available = 1" if only_available else "")
    return conn.execute(sql + " ORDER BY id", (item_id,)).fetchall()


def search_menu(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[sqlite3.Row]:
    """Simple keyword match over available items. Semantic search arrives with RAG in Phase 3."""
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return conn.execute("SELECT * FROM menu_items WHERE available = 1 ORDER BY category, name LIMIT ?", (limit,)).fetchall()
    clauses = " OR ".join("(lower(name) LIKE ? OR lower(category) LIKE ? OR lower(description) LIKE ?)" for _ in terms)
    params: list = []
    for t in terms:
        like = f"%{t}%"
        params += [like, like, like]
    return conn.execute(
        f"SELECT * FROM menu_items WHERE available = 1 AND ({clauses}) ORDER BY category, name LIMIT ?",
        (*params, limit),
    ).fetchall()

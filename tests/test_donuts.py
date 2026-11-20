"""Owner, 2026-11-20: Filled Donut and Long John are separate items. The Filled Donut takes an
outside and a filling; the Long John is $2.09, so it's one of the Big Donuts."""

import json

import pytest

from app.database.connection import MIGRATIONS_DIR
from app.services import cart, menu
from app.services.menu import seed_menu
from tests.conftest import ROOT


@pytest.fixture
def real_menu(conn):
    seed_menu(conn, ROOT / "data/menu.json")
    return conn


def test_filled_donut_asks_outside_and_filling(real_menu, customer_id):
    with pytest.raises(cart.DetailsNeeded) as exc:
        cart.add_item(real_menu, customer_id, "filled_donut", 1)
    assert exc.value.questions == [
        "Which outside would you like on the Filled Donut: Glazed, Chocolate Icing or Powdered Sugar?",
        "Which filling would you like: Bavarian Cream, Blueberry, Raspberry, Lemon or Strawberry jelly?",
    ]
    with pytest.raises(cart.DetailsNeeded) as exc:
        cart.add_item(real_menu, customer_id, "filled_donut", 1, details={"outside": "glazed"})
    assert len(exc.value.questions) == 1 and "filling" in exc.value.questions[0]


@pytest.mark.parametrize("outside,filling,want", [
    ("powder sugar", "strawberry jelly", {"outside": "Powdered Sugar", "filling": "Strawberry"}),
    ("chocolate", "cream", {"outside": "Chocolate Icing", "filling": "Bavarian Cream"}),
    ("Glazed", "lemon", {"outside": "Glazed", "filling": "Lemon"}),
    ("chocolate icing", "raspberry jelly", {"outside": "Chocolate Icing", "filling": "Raspberry"}),
])
def test_any_outside_goes_with_any_filling(real_menu, customer_id, outside, filling, want):
    v = cart.add_item(real_menu, customer_id, "filled_donut", 2, details={"outside": outside, "filling": filling})
    assert v.lines[-1].details == want and v.lines[-1].unit_price_cents == 179


def test_long_john_is_209_and_a_big_donut(real_menu, customer_id):
    assert menu.get_item(real_menu, "long_john")["price_cents"] == 209
    v = cart.add_item(real_menu, customer_id, "long_john", 1, details={"flavor": "chocolate"})
    assert v.lines[-1].details == {"flavor": "Chocolate"}
    assert "Long John" in [r["name"] for r in menu.pool_items(real_menu, "donuts", 209)]
    v = cart.add_item(real_menu, customer_id, "half_dozen_big_donuts", 1,
                      details={"big_donuts": "2 maple long johns, 2 pinecones, 2 chocolate twists"})
    assert v.lines[-1].details["big_donuts"] == "2 Long John (Maple), 2 Pinecone, 2 Twist (Chocolate)"
    assert menu.get_item(real_menu, "filled_donut_long_john") is None


def test_migration_splits_the_live_menu_item(real_menu):
    # The live menu (edited in the portal) still has the combined item at the board price.
    real_menu.execute("DELETE FROM menu_items WHERE id IN ('long_john', 'filled_donut')")
    real_menu.execute(
        """INSERT INTO menu_items (id, name, category, description, price_cents, max_quantity, sort_order, choices)
           VALUES ('filled_donut_long_john', 'Filled Donut/Long John', 'donuts', 'x', 179, 60, 5, ?)""",
        (json.dumps([{"key": "flavor", "label": "Flavor", "options": ["Chocolate", "Maple"]}]),))
    sql = (MIGRATIONS_DIR / "0012_split_filled_donut_long_john.sql").read_text()
    real_menu.executescript(sql)
    real_menu.executescript(sql)  # safe to run twice
    rows = {r["id"]: r for r in real_menu.execute(
        "SELECT * FROM menu_items WHERE id IN ('long_john', 'filled_donut', 'filled_donut_long_john')")}
    assert rows["long_john"]["price_cents"] == 209 and rows["filled_donut"]["price_cents"] == 179
    assert rows["filled_donut_long_john"]["archived"] == 1 and rows["filled_donut_long_john"]["available"] == 0
    assert [c["key"] for c in json.loads(rows["filled_donut"]["choices"])] == ["outside", "filling"]
    assert rows["long_john"]["sort_order"] == 5

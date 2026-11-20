"""Sanity checks on the real menu file the store runs with."""

import json

from app.services import cart, menu
from app.services.menu import seed_menu
from tests.conftest import ROOT

REAL_MENU = ROOT / "data/menu.json"


def test_real_menu_seeds_and_prices_are_sane(conn):
    seed_menu(conn, REAL_MENU)
    data = json.loads(REAL_MENU.read_text())
    ids = [i["id"] for i in data["items"]]
    assert len(ids) == len(set(ids))
    for it in data["items"]:
        assert 0 < it["price_cents"] < 5000, it["id"]
    # Spot-check a few prices from the menu board.
    assert menu.get_item(conn, "dozen_glazed_donuts")["price_cents"] == 1199
    assert menu.get_item(conn, "apple_fritter")["price_cents"] == 290
    assert menu.get_item(conn, "boudin_cheese_kolache")["price_cents"] == 365
    # Fixture-only items are retired, not deleted.
    assert menu.get_item(conn, "glazed_dozen")["available"] == 0


def test_real_menu_extras_price_correctly(conn, customer_id):
    seed_menu(conn, REAL_MENU)
    view = cart.add_item(
        conn, customer_id, "bacon_egg_cheese_croissant", 2,
        ["bacon_egg_cheese_croissant_extra_bacon", "bacon_egg_cheese_croissant_extra_cheese"],
    )
    assert view.lines[0].unit_price_cents == 425 + 99 + 50
    assert view.totals.subtotal_cents == 2 * 574


def test_real_hours():
    from datetime import date, time

    from app.services.business import load_business

    biz = load_business(ROOT / "data/business.json")
    assert biz.hours_on(date(2026, 11, 22)) == (time(4, 30), time(12, 30))  # a Sunday

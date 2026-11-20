"""Menu editing from the portal: live changes, categories, and no re-import over edits."""

import pytest

from app.database.bootstrap import prepare
from app.services import cart, menu, menu_admin
from app.services import order as order_svc
from tests.conftest import NOW
from tests.test_admin import logged_in, portal  # noqa: F401 - fixtures
from tests.test_orders import PICKUP


def test_categories_seeded_with_names(conn):
    cats = {c["id"]: c["name"] for c in menu_admin.categories(conn)}
    assert cats and all(cats.values())
    assert cats["donuts"] == "Donuts" and cats["special_design"] == "Special Design"


def test_parse_price():
    assert menu_admin.parse_price("12.99") == 1299
    assert menu_admin.parse_price("$1,000") == 100000
    assert menu_admin.parse_price("0") == 0
    for bad in ("abc", "-1", "1.999", "5000"):
        with pytest.raises(menu_admin.MenuError):
            menu_admin.parse_price(bad)


def test_price_change_is_live_and_guards_open_summary(conn, customer_id, biz, settings):
    cart.add_item(conn, customer_id, "glazed_dozen", 1)
    cart.set_pickup_details(conn, customer_id, pickup_time=PICKUP, customer_name="Jo", customer_phone="225-555-0123")
    s = order_svc.present_order_summary(conn, customer_id, biz, settings, NOW)
    menu_admin.update_item(conn, "glazed_dozen", {"price": "15.50", "available": "on"})
    assert menu.get_item(conn, "glazed_dozen")["price_cents"] == 1550
    with pytest.raises(order_svc.SummaryChanged):
        order_svc.submit_order(conn, customer_id, s.token, biz, settings, NOW)


def test_add_rename_move_delete_category(conn):
    cat = menu_admin.add_category(conn, "Seasonal Specials")
    assert cat == "seasonal_specials"
    with pytest.raises(menu_admin.MenuError):
        menu_admin.add_category(conn, "seasonal specials")
    menu_admin.rename_category(conn, cat, "Holiday Specials")
    item = menu_admin.add_item(conn, cat, "Pumpkin Donut", "1.75")
    assert item in [r["id"] for r in menu.search_menu(conn, "holiday")]  # found by category name
    with pytest.raises(menu_admin.MenuError, match="Move or remove"):
        menu_admin.delete_category(conn, cat)
    menu_admin.remove_item(conn, item)
    menu_admin.delete_category(conn, cat)
    assert cat not in [c["id"] for c in menu_admin.categories(conn)]
    first = menu_admin.categories(conn)[0]["id"]
    menu_admin.move_category(conn, first, 1)
    assert menu_admin.categories(conn)[1]["id"] == first


def test_hide_and_remove_item(conn, customer_id):
    menu_admin.set_available(conn, "glazed_dozen", False)
    assert "glazed_dozen" not in [r["id"] for r in menu.search_menu(conn, "glazed")]
    menu_admin.set_available(conn, "glazed_dozen", True)
    cart.add_item(conn, customer_id, "glazed_dozen", 1)
    menu_admin.remove_item(conn, "glazed_dozen")  # in a cart: archived, not deleted
    row = menu.get_item(conn, "glazed_dozen")
    assert row["archived"] == 1 and row["available"] == 0
    assert "Dozen Glazed Donuts" in cart.view_cart(conn, customer_id, 0).unavailable


def test_add_ons_edit_and_add(conn):
    item = next(r["item_id"] for r in conn.execute("SELECT item_id FROM menu_modifiers LIMIT 1"))
    mods = menu.get_modifiers(conn, item, only_available=False)
    m = mods[0]
    form = {"price": "5.00", "available": "on", f"mod_{m['id']}_name": "Renamed", f"mod_{m['id']}_price": "0.75",
            "new_mod_name": "Extra Cheese", "new_mod_price": "0.50"}
    menu_admin.update_item(conn, item, form)
    after = {x["name"]: x for x in menu.get_modifiers(conn, item, only_available=False)}
    assert after["Renamed"]["price_delta_cents"] == 75 and after["Renamed"]["available"] == 0
    assert after["Extra Cheese"]["price_delta_cents"] == 50


def test_edits_survive_restart_until_reimport(conn, settings):
    menu_admin.update_item(conn, "glazed_dozen", {"price": "20.00", "available": "on"})
    prepare(settings, conn)  # simulated restart
    assert menu.get_item(conn, "glazed_dozen")["price_cents"] == 2000
    menu.seed_menu(conn, settings.menu_path)
    assert menu.get_item(conn, "glazed_dozen")["price_cents"] != 2000


def test_export_round_trips(conn, tmp_path, settings):
    import json

    menu_admin.add_category(conn, "Seasonal")
    menu_admin.add_item(conn, "seasonal", "Pumpkin Donut", "1.75")
    data = menu_admin.export_menu(conn)
    p = tmp_path / "menu.json"
    p.write_text(json.dumps(data))
    menu.seed_menu(conn, p)
    assert menu.get_item(conn, "pumpkin_donut")["price_cents"] == 175
    assert {c["id"]: c["name"] for c in menu_admin.categories(conn)}["seasonal"] == "Seasonal"


# ---- portal pages

def test_menu_page_and_edits(logged_in):  # noqa: F811
    r = logged_in.get("/admin/menu")
    assert r.status_code == 200 and "Add category" in r.text and "Dozen Glazed Donuts" in r.text
    r = logged_in.post("/admin/menu/items/glazed_dozen",
                       data={"action": "save", "name": "Dozen Glazed Donuts", "price": "13.49", "category": "donuts",
                             "available": "on", "unit_label": "dozen", "max_quantity": "30", "description": ""},
                       headers={"HX-Request": "true"})
    assert r.status_code == 200 and "$13.49" in r.text
    r = logged_in.post("/admin/menu/items/glazed_dozen", data={"price": "abc", "name": "x", "category": "donuts"},
                       headers={"HX-Request": "true"})
    assert "number" in r.text
    r = logged_in.post("/admin/menu/categories", data={"name": "Seasonal"}, headers={"HX-Request": "true"})
    assert r.status_code == 204 and "/admin/menu" in r.headers["HX-Redirect"]
    r = logged_in.post("/admin/menu/items", data={"category": "seasonal", "name": "Pumpkin Donut", "price": "1.75"},
                       headers={"HX-Request": "true"})
    assert r.status_code == 204
    page = logged_in.get("/admin/menu").text
    assert "Pumpkin Donut" in page and "Re-import" in page
    r = logged_in.get("/admin/menu/export")
    assert r.json()["items"] and r.headers["content-disposition"].startswith("attachment")
    audit = logged_in.get("/admin/audit").text
    assert "menu.item.save" in audit


def test_menu_requires_csrf(logged_in):  # noqa: F811
    del logged_in.headers["X-CSRF-Token"]
    r = logged_in.post("/admin/menu/categories", data={"name": "X"})
    assert r.status_code in (403, 400)

"""Owner rules for bubble tea: Milk Tea and Green Tea ask how sweet, no drink comes with a
topping, and Sprinkle offers toppings once, leading with tapioca pearls."""

import asyncio
import json

import pytest

from app.agent import tools
from app.agent.prompts import system_prompt
from app.database.connection import MIGRATIONS_DIR
from app.services import cart
from app.services import order as order_svc
from app.services.menu import seed_menu
from app.timeutil import local
from tests.conftest import NOW, ROOT
from tests.test_owner_flow import PICKUP

SWEETNESS = ["Less sweet", "Regular", "More sweet"]


@pytest.fixture
def real_menu(conn):
    seed_menu(conn, ROOT / "data/menu.json")
    return conn


def _ex(conn, cid, settings, biz, name, args):
    return asyncio.run(tools.execute(tools.ToolContext(conn, cid, settings, biz, NOW), name, args))


@pytest.mark.parametrize("item_id,flavor", [("milk_tea", "Taro Milk Tea"), ("green_tea", "Mango Green Tea")])
def test_milk_and_green_tea_ask_how_sweet(real_menu, customer_id, item_id, flavor):
    with pytest.raises(cart.DetailsNeeded) as exc:
        cart.add_item(real_menu, customer_id, item_id, 1, details={"flavor": flavor})
    assert exc.value.questions == ["How sweet would you like it: Less sweet, Regular or More sweet?"]
    v = cart.add_item(real_menu, customer_id, item_id, 1, details={"flavor": flavor, "sweetness": "less"})
    assert v.lines[-1].details == {"flavor": flavor, "sweetness": "Less sweet"}
    assert v.lines[-1].modifiers == ()  # no topping unless asked


def test_other_drinks_dont_ask_sweetness(real_menu, customer_id):
    v = cart.add_item(real_menu, customer_id, "smoothie", 1, details={"flavor": "Mango Smoothie"})
    assert v.lines[-1].details == {"flavor": "Mango Smoothie"}


def test_sweetness_shows_on_summary_and_owner_alert(real_menu, customer_id, settings, biz):
    cart.add_item(real_menu, customer_id, "milk_tea", 1, details={"flavor": "Taro Milk Tea", "sweetness": "More sweet"})
    cart.set_pickup_details(real_menu, customer_id, pickup_time=PICKUP, customer_name="Jo", customer_phone="225-555-0123")
    s = order_svc.present_order_summary(real_menu, customer_id, biz, settings, NOW)
    assert "Sweetness: More sweet" in s.text
    o = order_svc.submit_order(real_menu, customer_id, s.token, biz, settings, NOW)
    assert "Sweetness: More sweet" in order_svc.format_owner_sms(real_menu, o, biz)


def test_adding_a_bubble_tea_suggests_tapioca_once(real_menu, customer_id, settings, biz):
    r = _ex(real_menu, customer_id, settings, biz, "add_to_cart",
            {"item_id": "green_tea", "quantity": 1, "details": {"flavor": "Peach Green Tea", "sweetness": "Regular"}})
    assert "Tapioca Pearl (+$0.80" in r["next_step"] and "green_tea_tapioca_pearl" in r["next_step"]
    assert r["cart"]["subtotal_before_tax"] == "$4.99"  # nothing added on its own
    # with a topping already picked, or for a non-bubble-tea item, there's nothing to suggest
    r = _ex(real_menu, customer_id, settings, biz, "add_to_cart",
            {"item_id": "iced_coffee", "quantity": 1, "details": {"flavor": "Vanilla Iced Coffee"},
             "modifier_ids": ["iced_coffee_tapioca_pearl"]})
    assert "next_step" not in r
    r = _ex(real_menu, customer_id, settings, biz, "add_to_cart", {"item_id": "glazed_donut", "quantity": 1})
    assert "next_step" not in r


def test_prompt_has_the_drink_rules(biz):
    prompt = system_prompt(biz, local(NOW, biz.timezone))
    assert "Less sweet, Regular or More sweet" in prompt
    assert "No drink comes with a topping" in prompt
    assert "offer toppings once" in prompt and "Tapioca Pearl" in prompt


def test_migration_adds_sweetness_to_an_edited_menu(real_menu):
    # The live menu was edited in the portal: its rows have no sweetness choice yet.
    for item_id in ("milk_tea", "green_tea"):
        row = real_menu.execute("SELECT choices FROM menu_items WHERE id = ?", (item_id,)).fetchone()
        kept = [c for c in json.loads(row["choices"]) if c["key"] != "sweetness"]
        real_menu.execute("UPDATE menu_items SET choices = ? WHERE id = ?", (json.dumps(kept), item_id))
    sql = (MIGRATIONS_DIR / "0010_drink_sweetness.sql").read_text()
    real_menu.executescript(sql)
    real_menu.executescript(sql)  # never added twice
    for item_id in ("milk_tea", "green_tea"):
        choices = json.loads(real_menu.execute("SELECT choices FROM menu_items WHERE id = ?", (item_id,)).fetchone()[0])
        assert [c["key"] for c in choices] == ["flavor", "sweetness"]
        assert choices[1]["options"] == SWEETNESS
    smoothie = real_menu.execute("SELECT choices FROM menu_items WHERE id = 'smoothie'").fetchone()[0]
    assert "sweetness" not in smoothie

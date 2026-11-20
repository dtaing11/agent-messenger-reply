"""Owner, 2026-11-20: toppings only go on Green Tea, Milk Tea, Iced Coffee and Smoothie. Every
other drink (Coffee, Cappuccino, milk, soda, water...) can't have them. The Iced Coffee is the
house blend, never "Community Coffee" (only Coffee and Cappuccino are)."""

import asyncio
from pathlib import Path

import pytest

from app.agent import reply_check, tools
from app.agent.prompts import system_prompt
from app.database.connection import connect, migrate
from app.services import cart, menu
from app.services import conversation as conv
from app.timeutil import local
from tests.conftest import NOW
from tests.test_agent import ScriptedLLM, run_agent

ROOT = Path(__file__).resolve().parent.parent
TOPPING_DRINKS = {"green_tea", "milk_tea", "iced_coffee", "smoothie"}


@pytest.fixture
def real(settings):
    c = connect(settings.database_path)
    migrate(c)
    menu.seed_menu(c, ROOT / "data/menu.json")
    yield c
    c.close()


@pytest.fixture
def cid(real):
    return conv.get_or_create_customer(real, "PAGE1", "PSID1")["id"]


def ex(conn, cid, settings, biz, name, args):
    return asyncio.run(tools.execute(tools.ToolContext(conn, cid, settings, biz, NOW), name, args))


def test_only_the_four_bubble_tea_drinks_have_toppings(real):
    drinks = [r["id"] for r in real.execute("SELECT id FROM menu_items WHERE category IN ('drinks', 'bubble_tea')")]
    assert TOPPING_DRINKS <= set(drinks) and len(drinks) > 10
    for item_id in drinks:
        names = [m["name"] for m in menu.get_modifiers(real, item_id)]
        if item_id in TOPPING_DRINKS:
            assert "Tapioca Pearl" in names, item_id
        else:
            assert names == [], item_id


def test_cart_refuses_a_topping_on_other_drinks(real, cid, settings, biz):
    out = ex(real, cid, settings, biz, "add_to_cart",
             {"item_id": "coffee", "quantity": 1, "modifier_ids": ["milk_tea_tapioca_pearl"]})
    assert "error" in out and "not available" in out["error"]
    with pytest.raises(cart.CartError):
        cart.add_item(real, cid, "water", 1, ["milk_tea_tapioca_pearl"])
    out = ex(real, cid, settings, biz, "search_menu", {"query": "coffee"})
    coffee = next(i for i in out["items"] if i["item_id"] == "coffee")
    assert "add_ons" not in coffee


def test_tapioca_is_offered_only_on_the_four(real, cid, settings, biz):
    for item_id in ("coffee", "cappuccino", "large_milk", "soft_drink"):
        out = ex(real, cid, settings, biz, "add_to_cart", {"item_id": item_id, "quantity": 1})
        assert out["ok"] and "next_step" not in out, item_id
    out = ex(real, cid, settings, biz, "add_to_cart",
             {"item_id": "iced_coffee", "quantity": 1, "details": {"flavor": "Caramel Iced Coffee"}})
    assert "Tapioca" in out["next_step"]
    out = ex(real, cid, settings, biz, "add_to_cart",
             {"item_id": "iced_coffee", "quantity": 1, "modifier_ids": ["iced_coffee_tapioca_pearl"],
              "details": {"flavor": "Vanilla Iced Coffee"}})
    assert out["ok"]


def test_iced_coffee_is_the_house_blend(real, cid, settings, biz):
    out = ex(real, cid, settings, biz, "search_menu", {"query": "iced coffee"})
    iced = next(i for i in out["items"] if i["item_id"] == "iced_coffee")
    assert "house blend" in iced["description"] and "add_ons" in iced
    assert "Community Coffee" in next(i for i in out["items"] if i["item_id"] == "coffee")["description"]
    prompt = system_prompt(biz, local(NOW, biz.timezone))
    assert "Never say the Iced Coffee is Community Coffee" in prompt
    assert "Toppings only go on Green Tea, Milk Tea, Iced Coffee and Smoothie" in prompt


@pytest.mark.parametrize("text, flagged", [
    ("Our Iced Coffee is made with Community Coffee!", True),
    ("Yes, the iced coffee uses Community Coffee.", True),
    ("Community Coffee iced coffee, coming up!", True),
    ("Our Coffee and Cappuccino are made with Community Coffee, and the Iced Coffee is our house blend.", False),
    ("Coffee is Community Coffee while the iced coffee is our house blend.", False),
    ("The iced coffee isn't Community Coffee.", False),
    ("Coffee is made with Community Coffee. Iced Coffee comes in Caramel or Vanilla.", False),
])
def test_reply_check_catches_iced_coffee_called_community(conn, customer_id, text, flagged):
    assert ("blend" in reply_check.problems(conn, customer_id, text)) is flagged


def test_a_reply_calling_iced_coffee_community_coffee_is_rewritten(conn, customer_id, settings, biz):
    conv.record_message(conn, customer_id, "customer", "what coffee is in the iced coffee?")
    llm = ScriptedLLM({"content": "Our Iced Coffee is made with Community Coffee!"},
                      {"content": "Our Iced Coffee is made with our house blend."})
    r = run_agent(conn, customer_id, settings, biz, llm)
    assert r.text == "Our Iced Coffee is made with our house blend."
    assert any("house blend, not Community Coffee" in str(m.get("content")) for m in llm.seen[1])
    # Still wrong after one rewrite: a fixed, correct reply goes out instead.
    conv.record_message(conn, customer_id, "customer", "so it's community coffee?")
    llm = ScriptedLLM({"content": "Yes, the iced coffee is Community Coffee."},
                      {"content": "Yep, iced coffee uses Community Coffee."})
    r = run_agent(conn, customer_id, settings, biz, llm)
    assert r.text.startswith("Just so you know, our Iced Coffee is made with our house blend.")


def test_migration_hides_drink_toppings_on_the_live_menu(settings):
    """The Mac's menu was edited in the portal: 0014 must fix the database directly."""
    c = connect(settings.database_path)
    migrate(c)
    old_iced = (
        "Bubble tea menu. Toppings (add any number): Coconut Jelly, Mango, Peach, Passion Fruit or Strawberry "
        "Bursting Boba, Tapioca Pearl (+$0.80 each), Coffee Jelly (+$1.25).")
    c.executescript(f"""
        INSERT INTO menu_items (id, name, category, description, price_cents) VALUES
          ('coffee', 'Coffee', 'drinks', 'Made with Community Coffee.', 259),
          ('water', 'Water', 'drinks', '', 165),
          ('iced_coffee', 'Iced Coffee', 'bubble_tea', '{old_iced}', 399),
          ('milk_tea', 'Milk Tea', 'bubble_tea', 'Owner wrote this', 499);
        INSERT INTO menu_modifiers (id, item_id, name, price_delta_cents, available) VALUES
          ('coffee_tapioca_pearl', 'coffee', 'Tapioca Pearl', 80, 1),
          ('water_tapioca_pearl', 'water', 'Tapioca Pearl', 80, 1),
          ('iced_coffee_tapioca_pearl', 'iced_coffee', 'Tapioca Pearl', 80, 1),
          ('milk_tea_tapioca_pearl', 'milk_tea', 'Tapioca Pearl', 80, 1);
    """)
    c.executescript((ROOT / "app/database/migrations/0014_drink_toppings_and_iced_coffee.sql").read_text())
    avail = {r["id"]: r["available"] for r in c.execute("SELECT id, available FROM menu_modifiers")}
    assert avail == {"coffee_tapioca_pearl": 0, "water_tapioca_pearl": 0,
                     "iced_coffee_tapioca_pearl": 1, "milk_tea_tapioca_pearl": 1}
    iced = menu.get_item(c, "iced_coffee")
    assert iced["description"].startswith("Iced coffee made with our house blend (not Community Coffee).")
    # An owner-written Iced Coffee description is kept, with the fact added in front.
    c.execute("UPDATE menu_items SET description = 'Owner text' WHERE id = 'iced_coffee'")
    c.executescript((ROOT / "app/database/migrations/0014_drink_toppings_and_iced_coffee.sql").read_text())
    assert menu.get_item(c, "iced_coffee")["description"] == "Made with our house blend (not Community Coffee). Owner text"
    c.close()

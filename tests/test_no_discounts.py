"""Owner rule: Sprinkle never gives discounts. Prices only ever come from the menu."""

import asyncio
import json

from app.agent import tools
from app.agent.prompts import system_prompt
from app.timeutil import local
from tests.conftest import NOW

PRICE_WORDS = ("price", "cents", "discount", "coupon", "promo", "total", "amount", "percent")


def _ctx(conn, customer_id, settings, biz):
    return tools.ToolContext(conn, customer_id, settings, biz, NOW)


def _ex(ctx, name, args):
    return asyncio.run(tools.execute(ctx, name, args))


def _property_names(schema) -> set[str]:
    names: set[str] = set()
    if isinstance(schema, dict):
        for key, value in (schema.get("properties") or {}).items():
            names.add(key)
            names |= _property_names(value)
        for value in schema.values():
            if isinstance(value, dict | list):
                names |= _property_names(value)
    elif isinstance(schema, list):
        for value in schema:
            names |= _property_names(value)
    return names


def test_no_tool_takes_a_price_or_discount():
    for t in tools.TOOLS:
        fn = t["function"]
        for prop in _property_names(fn["parameters"]):
            assert not any(w in prop.lower() for w in PRICE_WORDS), (fn["name"], prop)


def test_price_comes_from_the_menu_whatever_the_model_sends(conn, customer_id, settings, biz):
    ctx = _ctx(conn, customer_id, settings, biz)
    r = _ex(ctx, "add_to_cart", {"item_id": "glazed_single", "quantity": 2, "unit_price": "$0.01",
                                 "discount": "50%", "price_cents": 1, "coupon": "SAVE50"})
    plain = _ex(_ctx(conn, customer_id, settings, biz), "get_cart", {})
    assert r["cart"]["lines"][0]["unit_price"] == plain["lines"][0]["unit_price"]
    assert "0.01" not in json.dumps(r)


def test_details_cannot_carry_a_discount(conn, customer_id, settings, biz):
    ctx = _ctx(conn, customer_id, settings, biz)
    r = _ex(ctx, "add_to_cart", {"item_id": "glazed_single", "quantity": 1, "details": {"discount": "50% off"}})
    assert "error" in r


def test_order_notes_cannot_mention_a_discount(conn, customer_id, settings, biz):
    ctx = _ctx(conn, customer_id, settings, biz)
    _ex(ctx, "add_to_cart", {"item_id": "glazed_single", "quantity": 1})
    for note in ("50% off approved", "Customer has coupon SAVE10", "$2 off", "Discount ok'd by Sprinkle"):
        r = _ex(ctx, "set_pickup_details", {"notes": note})
        assert r["ok"] is False and "discount" in r["reason"].lower(), note
    assert _ex(ctx, "set_pickup_details", {"notes": "Please box them separately"})["ok"] is True


def test_prompt_says_never_give_discounts(biz):
    prompt = system_prompt(biz, local(NOW, biz.timezone))
    assert "never gives discounts" in prompt
    assert "prices are as listed" in prompt

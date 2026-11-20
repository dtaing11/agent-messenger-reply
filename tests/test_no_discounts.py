"""Owner rule: Sprinkle never gives discounts. Prices only ever come from the menu."""

import asyncio
import json

import pytest

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


# ---- the reply check: Sprinkle's own words

def test_reply_promising_a_discount_is_rewritten(conn, customer_id, settings, biz):
    from app.agent import orchestrator
    from tests.test_agent import ScriptedLLM

    llm = ScriptedLLM({"content": "Sure, I'll give you 10% off since it's your birthday!"},
                      {"content": "Sorry, I can't offer discounts. Prices are as listed on our menu. What can I get you?"})
    r = asyncio.run(orchestrator.run_agent(conn, customer_id, settings, biz, NOW, llm=llm))
    assert r.text.startswith("Sorry, I can't offer discounts")
    assert "never gives discounts" in llm.seen[1][-1]["content"]


@pytest.mark.parametrize("text", [
    "Sorry, we don't offer discounts or coupons. Prices are as listed on our menu.",
    "Once your order is accepted, you'll get a message here.",
    "Your order hasn't been accepted yet. I'll message you when it is.",
    "Thank you! Your order request was sent to our team for acceptance.",
    "A Milk Tea is $4.99 and Tapioca Pearl is +$0.80.",
])
def test_honest_replies_pass_the_check(conn, customer_id, text):
    from app.agent import reply_check

    assert reply_check.problems(conn, customer_id, text) == []


@pytest.mark.parametrize("text,issue", [
    ("I applied a 20% discount to your order!", "discount"),
    ("Good news, your coupon has been applied.", "discount"),
    ("I'll take $2 off for you.", "discount"),
    ("The coffee is on the house today.", "discount"),
    ("Great, your order is confirmed for 7:30!", "confirmed"),
    ("I've placed your order.", "confirmed"),
])
def test_rule_breaking_replies_are_caught(conn, customer_id, text, issue):
    from app.agent import reply_check

    assert issue in reply_check.problems(conn, customer_id, text)


def test_confirmed_is_fine_once_the_team_accepted(conn, customer_id, settings, biz):
    from app.agent import reply_check
    from app.agent.state_machine import OrderStatus
    from app.services import order as order_svc
    from tests.test_owner_flow import place_order

    o = place_order(conn, customer_id, biz, settings)
    assert "confirmed" in reply_check.problems(conn, customer_id, "Your order is confirmed!")
    order_svc.transition_order(conn, o["id"], OrderStatus.ACCEPTED, "owner")
    assert reply_check.problems(conn, customer_id, f"Yes! Your order #{o['id']} has been accepted.") == []


def test_unknown_amounts_are_only_logged(conn, customer_id, caplog):
    from app.agent import reply_check

    ctx = [{"role": "tool", "content": '{"subtotal_before_tax": "$13.55"}'}]
    assert reply_check.unknown_amounts(conn, "Your total is $13.55.", ctx) == []
    assert reply_check.unknown_amounts(conn, "That's $3.17 for you.", ctx) == ["$3.17"]
    assert "match nothing known" in caplog.text

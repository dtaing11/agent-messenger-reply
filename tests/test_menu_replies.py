"""Easy-to-scan menu answers: a per-item price overview, tap-to-pick options, and the prompt rules."""

import asyncio

import pytest

from app.agent import orchestrator, tools
from app.agent.prompts import system_prompt
from app.integrations import messenger
from app.services import conversation as conv
from app.services import menu
from app.timeutil import local
from app.workers.message_worker import MessageProcessor, run_once
from tests.conftest import NOW, ROOT
from tests.test_agent import ScriptedLLM, call
from tests.test_worker import FakeResponder, enqueue_message


@pytest.fixture
def real_menu(conn):
    menu.seed_menu(conn, ROOT / "data/menu.json")
    return conn


def test_bubble_tea_overview_has_each_drinks_own_price(real_menu):
    r = menu.browse(real_menu, "bubble tea")
    prices = {i["name"]: i["price_before_tax"] for i in r["items"]}
    assert prices == {"Milk Tea": "$4.99", "Green Tea": "$4.99", "Smoothie": "$5.99", "Iced Coffee": "$3.99"}
    assert r["same_price"] is False and "Prices differ" in r["price_note"]
    assert all(i["has_choices"] for i in r["items"])


def test_category_list_and_unknown_category(real_menu):
    cats = {c["category"]: c for c in menu.browse(real_menu)["categories"]}
    assert cats["Bubble Tea"]["prices"] == "$3.99-$5.99" and cats["Bubble Tea"]["items"] == 4
    assert menu.browse(real_menu, "Bubble_Tea")["category"] == "Bubble Tea"
    r = menu.browse(real_menu, "pizza")
    assert "error" in r and "Bubble Tea" in r["categories"]


def test_same_price_flag_when_every_price_matches(conn):
    r = menu.browse(conn, "special design")
    assert r["same_price"] is True and r["price_note"] == "Every item here is $22.89."


def test_prompt_rules_for_short_menu_answers(biz):
    p = system_prompt(biz, local(NOW, biz.timezone))
    assert "Show one level at a time" in p and "browse_menu" in p
    assert 'Never say "all $X"' in p and "same_price is true" in p
    assert "Mention toppings or add-ons only after they've picked" in p
    assert "offer_choices" in p


def test_offer_choices_become_buttons_under_the_reply(conn, customer_id, settings, biz):
    conv.record_message(conn, customer_id, "customer", "what bubble tea do you have?")
    llm = ScriptedLLM(
        {"content": None, "tool_calls": [call("browse_menu", {"category": "Bubble Tea"}),
                                         call("offer_choices", {"options": ["Milk Tea", "Green Tea", "Smoothie", "milk tea"]}, "c2")]},
        {"content": "We have:\nMilk Tea $4.99\nGreen Tea $4.99\nSmoothie $5.99\nWhich one?"},
    )
    r = asyncio.run(orchestrator.run_agent(conn, customer_id, settings, biz, NOW, llm=llm))
    assert r.quick_replies == ("Milk Tea", "Green Tea", "Smoothie")
    assert r.text.endswith("Which one?")


def test_offer_choices_needs_two_options(conn, customer_id, settings, biz):
    c = tools.ToolContext(conn, customer_id, settings, biz, NOW)
    assert "at least 2" in asyncio.run(tools.execute(c, "offer_choices", {"options": ["Mango", "mango"]}))["error"]
    assert c.quick_replies == ()


def test_quick_reply_titles_fit_messenger_limits():
    q = messenger.quick_replies(["Milk Tea", "Chocolate Icing with Sprinkles"] + [f"Option {i}" for i in range(20)])
    assert len(q) == 13 and all(len(x["title"]) <= 20 for x in q)
    assert q[1]["title"].endswith("…") and q[1]["payload"] == "PICK:Chocolate Icing with Sprinkles"


def test_send_choices_puts_buttons_on_the_last_part(settings):
    sent = []

    class Client(messenger.MessengerClient):
        async def _send(self, payload):
            sent.append(payload)
            return {}

    asyncio.run(Client(settings, http=object()).send_choices("PSID1", "Which one?", ["Milk Tea", "Smoothie"]))
    assert sent[0]["message"]["text"] == "Which one?"
    assert [q["title"] for q in sent[0]["message"]["quick_replies"]] == ["Milk Tea", "Smoothie"]


def test_worker_sends_buttons_and_reads_a_tap_as_text(conn, settings, biz):
    responder, seen = FakeResponder(), []

    async def agent(conn, cid, s, b, now):
        seen.append(orchestrator._history(conn, cid, 16)[-1]["content"])
        return orchestrator.AgentResult(text="Which kind?", quick_replies=("Milk Tea", "Green Tea"))

    proc = MessageProcessor(settings, biz, responder, agent=agent)
    enqueue_message(conn, "m1", "bubble tea?")
    asyncio.run(run_once(conn, proc, settings, NOW))
    assert responder.sent[-1] == ("PSID1", "Which kind?", ("Milk Tea", "Green Tea"))
    enqueue_message(conn, "m2", "Chocolate Icing w…", quick_reply="PICK:Chocolate Icing with Sprinkles")
    asyncio.run(run_once(conn, proc, settings, NOW))
    assert seen == ["bubble tea?", "Chocolate Icing with Sprinkles"]


def test_summary_or_handoff_drops_buttons(conn, customer_id, settings, biz):
    c = tools.ToolContext(conn, customer_id, settings, biz, NOW)
    c.quick_replies = ("A", "B")
    c.handoff_reason = "needs a person"
    assert orchestrator._finish(c, "Someone will help").quick_replies == ()

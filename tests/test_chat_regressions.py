"""Regressions from the owner's test chat on 2026-11-20 (catering order for 12):

- Sprinkle offered buttons for splitting 10 sandwiches, the customer tapped "5 biscuit / 5 burri…",
  and Sprinkle answered "Sorry, could you say that another way?" (empty model reply).
- The button label was cut off (Messenger shows at most 20 characters).
- "Mix of both" for 10 sandwiches needs an exact count per menu item.
"""

import asyncio

import pytest

from app.agent import orchestrator, tools
from app.agent.prompts import system_prompt
from app.services import cart, menu
from app.services import conversation as conv
from app.timeutil import local
from app.workers.message_worker import MessageProcessor, run_once
from tests.conftest import NOW, ROOT
from tests.test_agent import ScriptedLLM, call
from tests.test_worker import FakeResponder, enqueue_message

SPLIT_QUESTION = "How would you like the 10 sandwiches split?"
BUTTONS = ("5 biscuit, 5 burrito", "All biscuits", "All burritos")


def test_tapped_button_reaches_the_ai_with_the_buttons_it_offered(conn, settings, biz):
    responder, seen = FakeResponder(), []
    replies = [orchestrator.AgentResult(text=SPLIT_QUESTION, quick_replies=BUTTONS),
               orchestrator.AgentResult(text="Got it!")]

    async def agent(conn, cid, s, b, now):
        seen.append(orchestrator._history(conn, cid, 16))
        return replies.pop(0)

    proc = MessageProcessor(settings, biz, responder, agent=agent)
    enqueue_message(conn, "m1", "Mix of both")
    asyncio.run(run_once(conn, proc, settings, NOW))
    assert responder.sent[-1] == ("PSID1", SPLIT_QUESTION, BUTTONS)
    # Messenger sends the (possibly cut) title as text and our payload with the full option
    enqueue_message(conn, "m2", "5 biscuit, 5 burrito", quick_reply="PICK:5 biscuit, 5 burrito")
    asyncio.run(run_once(conn, proc, settings, NOW))
    history = seen[-1]
    assert history[-2] == {"role": "assistant",
                           "content": f"{SPLIT_QUESTION}\n[Buttons shown: 5 biscuit, 5 burrito | All biscuits | All burritos]"}
    assert history[-1] == {"role": "user", "content": "5 biscuit, 5 burrito"}
    assert responder.sent[-1][1] == "Got it!"


def test_empty_model_reply_is_retried_not_sent_as_sorry(conn, customer_id, settings, biz):
    conv.record_message(conn, customer_id, "assistant", f"{SPLIT_QUESTION}\n[Buttons shown: 5 biscuit, 5 burrito | All biscuits]")
    conv.record_message(conn, customer_id, "customer", "5 biscuit, 5 burrito")
    llm = ScriptedLLM({"content": "", "finish_reason": "length"},
                      {"content": "Here's a split: 2 Sausage Biscuits, 2 Sausage Burritos ... Sound good?"})
    r = asyncio.run(orchestrator.run_agent(conn, customer_id, settings, biz, NOW, llm=llm))
    assert r.text.startswith("Here's a split") and r.text != orchestrator.NOT_UNDERSTOOD
    assert llm.seen[1][-1] == {"role": "system", "content": orchestrator.EMPTY_NUDGE}


def test_empty_twice_still_falls_back(conn, customer_id, settings, biz):
    conv.record_message(conn, customer_id, "customer", "5 biscuit, 5 burrito")
    llm = ScriptedLLM({"content": ""}, {"content": "  "})
    r = asyncio.run(orchestrator.run_agent(conn, customer_id, settings, biz, NOW, llm=llm))
    assert r.text == orchestrator.NOT_UNDERSTOOD and len(llm.seen) == 2


def test_button_labels_must_fit_messenger(conn, customer_id, settings, biz):
    c = tools.ToolContext(conn, customer_id, settings, biz, NOW)
    r = asyncio.run(tools.execute(c, "offer_choices", {"options": ["5 biscuit / 5 burrito", "All biscuits"]}))
    assert "20 characters or fewer" in r["error"] and "5 biscuit / 5 burrito" in r["error"] and c.quick_replies == ()
    r = asyncio.run(tools.execute(c, "offer_choices", {"options": ["5 biscuit, 5 burrito", "All | biscuits"]}))
    assert r["ok"] and c.quick_replies == ("5 biscuit, 5 burrito", "All / biscuits")


def test_model_never_shows_the_buttons_note(conn, customer_id, settings, biz):
    conv.record_message(conn, customer_id, "customer", "hi")
    llm = ScriptedLLM({"content": "Which one?\n[Buttons shown: A | B]"})
    r = asyncio.run(orchestrator.run_agent(conn, customer_id, settings, biz, NOW, llm=llm))
    assert r.text == "Which one?"


def test_retry_resends_reply_with_its_buttons(conn, settings, biz):
    responder = FakeResponder(fail_times=1)
    calls = []

    async def agent(conn, cid, s, b, now):
        calls.append(1)
        return orchestrator.AgentResult(text=SPLIT_QUESTION, quick_replies=BUTTONS)

    proc = MessageProcessor(settings, biz, responder, agent=agent)

    async def flaky_choices(psid, text, options, _state={"n": 0}):
        _state["n"] += 1
        if _state["n"] == 1:
            raise RuntimeError("send failed")
        responder.sent.append((psid, text, tuple(options)))

    responder.send_choices = flaky_choices
    enqueue_message(conn, "m1", "Mix of both")
    asyncio.run(run_once(conn, proc, settings, NOW))
    asyncio.run(run_once(conn, proc, settings, NOW.replace(minute=1)))
    assert calls == [1] and responder.sent == [("PSID1", SPLIT_QUESTION, BUTTONS)]


def test_split_buttons_note_round_trip():
    saved = orchestrator.with_buttons_note("Pick one", ("A", "B"))
    assert orchestrator.split_buttons_note(saved) == ("Pick one", ["A", "B"])
    assert orchestrator.split_buttons_note("no buttons") == ("no buttons", [])


def test_prompt_rules_from_the_test_chat(biz):
    p = system_prompt(biz, local(NOW, biz.timezone))
    assert "Biscuits and burritos are separate menu items" in p and "exact count of each menu item" in p
    assert "propose one exact split" in p
    assert "20 characters or fewer" in p and "[Buttons shown: ...]" in p
    assert "Never make up a tax rate yourself" in p


@pytest.fixture
def real_menu(conn):
    menu.seed_menu(conn, ROOT / "data/menu.json")
    return conn


def test_sandwich_split_adds_six_lines_at_36_50(real_menu, customer_id, settings, biz):
    """4 sausage, 3 bacon, 3 ham as a mix of biscuits and burritos: one cart line per menu item."""
    conv.record_message(real_menu, customer_id, "customer", "Sounds good")
    split = {"sausage_egg_cheese_biscuit": 2, "sausage_egg_cheese_burrito": 2, "bacon_egg_cheese_biscuit": 2,
             "bacon_egg_cheese_burrito": 1, "ham_egg_cheese_biscuit": 1, "ham_egg_cheese_burrito": 2}
    llm = ScriptedLLM(
        {"content": None, "tool_calls": [call("add_to_cart", {"item_id": k, "quantity": q}, f"c{i}")
                                         for i, (k, q) in enumerate(split.items())]},
        {"content": "Added all 10 sandwiches."},
    )
    r = asyncio.run(orchestrator.run_agent(real_menu, customer_id, settings, biz, NOW, llm=llm))
    assert r.text == "Added all 10 sandwiches."
    view = cart.view_cart(real_menu, customer_id)
    assert {l.item_id: l.quantity for l in view.lines} == split
    assert view.totals.subtotal_cents == 3650

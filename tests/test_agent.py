"""The AI tool loop, driven by a scripted fake LLM (no network)."""

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr

from app.agent import orchestrator, tools
from app.integrations import openrouter
from app.integrations.openrouter import LLMError
from app.services import cart
from app.services import conversation as conv
from app.workers import queue
from app.workers.message_worker import MessageProcessor, run_once
from tests.conftest import NOW
from tests.test_worker import FakeResponder, enqueue_message

PICKUP = datetime(2026, 11, 19, 7, 30, tzinfo=ZoneInfo("America/Chicago"))


def call(name, args=None, cid="c1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args or {})}}


class ScriptedLLM:
    """Returns the scripted assistant messages in order and records what it was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen: list[list[dict]] = []

    async def __call__(self, settings, messages, tool_defs):
        self.seen.append([dict(m) for m in messages])
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def run_agent(conn, cid, settings, biz, llm):
    return asyncio.run(orchestrator.run_agent(conn, cid, settings, biz, NOW, llm=llm))


def test_plain_answer(conn, customer_id, settings, biz):
    conv.record_message(conn, customer_id, "customer", "what time do you open?")
    llm = ScriptedLLM({"content": "We open at 4:30 AM every day."})
    r = run_agent(conn, customer_id, settings, biz, llm)
    assert r.text == "We open at 4:30 AM every day." and r.summary is None and r.handoff_reason is None
    msgs = llm.seen[0]
    assert msgs[0]["role"] == "system" and "Town Donut Watson" in msgs[0]["content"]
    assert msgs[-1] == {"role": "user", "content": "what time do you open?"}


def test_order_flow_ends_with_summary(conn, customer_id, settings, biz):
    conv.record_message(conn, customer_id, "customer", "2 dozen glazed for tomorrow 7:30am, name Jessica")
    llm = ScriptedLLM(
        {"content": None, "tool_calls": [call("search_menu", {"query": "glazed dozen"})]},
        {"content": "", "tool_calls": [
            call("add_to_cart", {"item_id": "glazed_dozen", "quantity": 2}, "c2"),
            call("set_pickup_details", {"pickup_time": "2026-11-19 07:30", "customer_name": "Jessica", "customer_phone": "(225) 555-0123"}, "c3"),
        ]},
        {"content": "Here's your order:", "tool_calls": [call("show_order_summary", {}, "c4")]},
    )
    r = run_agent(conn, customer_id, settings, biz, llm)
    assert r.summary is not None and r.text == "Here's your order:"
    assert "2 x Dozen Glazed Donuts" in r.summary.text
    assert "Town Donut Watson, Oak Point Plaza, next to Subway" in r.summary.text
    assert r.tools_used == ("search_menu", "add_to_cart", "set_pickup_details", "show_order_summary")
    # tool results were fed back with the matching ids
    tool_msgs = [m for m in llm.seen[2] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2", "c3"]
    assert "glazed_dozen" in [i["item_id"] for i in json.loads(tool_msgs[0]["content"])["items"]]


def test_cart_is_shown_to_model(conn, customer_id, settings, biz):
    cart.add_item(conn, customer_id, "glazed_dozen", 1)
    conv.record_message(conn, customer_id, "customer", "anything else?")
    llm = ScriptedLLM({"content": "ok"})
    run_agent(conn, customer_id, settings, biz, llm)
    assert "Dozen Glazed Donuts" in llm.seen[0][1]["content"]


def test_bad_tool_args_become_error_results(conn, customer_id, settings, biz):
    conv.record_message(conn, customer_id, "customer", "add a unicorn")
    llm = ScriptedLLM(
        {"content": "", "tool_calls": [call("add_to_cart", {"item_id": "unicorn", "quantity": 1})]},
        {"content": "Sorry, we don't have that."},
    )
    r = run_agent(conn, customer_id, settings, biz, llm)
    assert r.text == "Sorry, we don't have that."
    err = json.loads([m for m in llm.seen[1] if m["role"] == "tool"][0]["content"])
    assert "error" in err


def test_design_donut_needs_details(conn, customer_id, settings, biz):
    ctx = tools.ToolContext(conn, customer_id, settings, biz, NOW)
    item = conn.execute("SELECT id FROM menu_items WHERE custom_rules IS NOT NULL LIMIT 1").fetchone()
    if item is None:
        pytest.skip("fixture menu has no custom item")
    res = asyncio.run(tools.execute(ctx, "add_to_cart", {"item_id": item["id"], "quantity": 1}))
    assert res["error"] == "details_needed" and res["ask_customer"]


def test_handoff_tool(conn, customer_id, settings, biz):
    conv.record_message(conn, customer_id, "customer", "I want a refund")
    llm = ScriptedLLM(
        {"content": "", "tool_calls": [call("request_human_support", {"reason": "refund request"})]},
        {"content": ""},
    )
    r = run_agent(conn, customer_id, settings, biz, llm)
    assert r.handoff_reason == "refund request" and r.text == orchestrator.HANDOFF_DEFAULT


def test_llm_error_reported(conn, customer_id, settings, biz):
    conv.record_message(conn, customer_id, "customer", "hi")
    r = run_agent(conn, customer_id, settings, biz, ScriptedLLM(LLMError("down")))
    assert r.error == "down"


def test_max_rounds_hands_off(conn, customer_id, settings, biz):
    conv.record_message(conn, customer_id, "customer", "hi")
    s = settings.model_copy(update={"max_tool_rounds": 2})
    llm = ScriptedLLM(*[{"content": "", "tool_calls": [call("get_cart")]}] * 2)
    r = run_agent(conn, customer_id, s, biz, llm)
    assert r.handoff_reason and r.text == orchestrator.TOO_MANY_STEPS


def test_history_starts_with_user_and_labels_staff(conn, customer_id):
    conv.record_message(conn, customer_id, "assistant", "earlier bot reply")
    conv.record_message(conn, customer_id, "customer", "hello")
    conv.record_message(conn, customer_id, "staff", "Hi, owner here")
    h = orchestrator._history(conn, customer_id, 10)
    assert h[0]["role"] == "user" and "Store team" in h[-1]["content"]


def test_request_body_privacy_and_tools(settings):
    s = settings.model_copy(update={"ai_deny_data_collection": True})
    body = openrouter.request_body(s, "deepseek/deepseek-v4.1-flash", [{"role": "user", "content": "x"}], tools.TOOLS)
    assert body["provider"]["data_collection"] == "deny" and body["provider"]["require_parameters"] is True
    assert body["tool_choice"] == "auto" and len(body["tools"]) == len(tools.TOOLS)


def test_ai_configured(settings):
    assert not orchestrator.ai_configured(settings)
    assert orchestrator.ai_configured(settings.model_copy(update={"openrouter_api_key": SecretStr("k"), "ai_model": "m"}))


# ---- worker integration

def _processor(settings, biz, responder, results):
    calls = []

    async def agent(conn, cid, s, b, now):
        calls.append(cid)
        r = results.pop(0)
        if callable(r):
            return r(conn, cid)
        return r

    return MessageProcessor(settings, biz, responder, agent=agent), calls


def test_worker_sends_summary_with_buttons(conn, settings, biz):
    responder = FakeResponder()

    def build(conn, cid):
        cart.add_item(conn, cid, "glazed_dozen", 1)
        cart.set_pickup_details(conn, cid, pickup_time=PICKUP, customer_name="Ann", customer_phone="225-555-0123")
        from app.services import order as order_svc
        s = order_svc.present_order_summary(conn, cid, biz, settings, NOW)
        return orchestrator.AgentResult(text="Here you go:", summary=s)

    proc, _ = _processor(settings, biz, responder, [build])
    enqueue_message(conn, "m1", "1 dozen glazed")
    asyncio.run(run_once(conn, proc, settings, NOW))
    assert responder.sent[0][1] == "Here you go:"
    assert responder.sent[1][2] and "Oak Point Plaza" in responder.sent[1][1]


def test_worker_retry_resends_without_rerunning_agent(conn, settings, biz):
    responder = FakeResponder(fail_times=1)
    proc, calls = _processor(settings, biz, responder, [orchestrator.AgentResult(text="Added!")])
    enqueue_message(conn, "m1", "add a dozen")
    asyncio.run(run_once(conn, proc, settings, NOW))
    asyncio.run(run_once(conn, proc, settings, NOW.replace(minute=1)))
    assert len(calls) == 1
    assert [t for _, t, _ in responder.sent] == ["Added!"]
    assert queue.stats(conn) == {"done": 1}


def test_worker_llm_error_apologizes_and_hands_off(conn, settings, biz):
    responder = FakeResponder()
    proc, _ = _processor(settings, biz, responder, [orchestrator.AgentResult(error="timeout")])
    enqueue_message(conn, "m1", "hi")
    asyncio.run(run_once(conn, proc, settings, NOW))
    assert "having trouble" in responder.sent[0][1]
    assert conn.execute("SELECT bot_enabled FROM conversations").fetchone()[0] == 0
    rows = conn.execute("SELECT type, recipient FROM notifications").fetchall()
    assert {r["recipient"] for r in rows} == set(settings.owner_phone_numbers) and rows[0]["type"] == "handoff"
    # the bot then stays quiet for this customer
    enqueue_message(conn, "m2", "hello?")
    asyncio.run(run_once(conn, proc, settings, NOW))
    assert len(responder.sent) == 1


def test_default_agent_uses_placeholder_without_key(conn, settings, biz):
    responder = FakeResponder()
    proc = MessageProcessor(settings, biz, responder)
    enqueue_message(conn, "m1", "hi")
    asyncio.run(run_once(conn, proc, settings, NOW))
    assert responder.sent[0][1] == settings.placeholder_reply

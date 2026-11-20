"""The eval set (evals/) stays valid, and its runner works end to end. A scripted fake model
stands in for OpenRouter here; real scores come from `python -m evals.run`."""

import asyncio
import json

from evals.dataset_check import check_dataset
from evals.runner import load_cases, run_case


def call(name, args, cid):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class Scripted:
    def __init__(self, *replies):
        self.replies = list(replies)

    async def __call__(self, settings, messages, tool_defs):
        return self.replies.pop(0) if self.replies else {"content": "Anything else I can help with?"}


def case(cid):
    return next(c for c in load_cases() if c["id"] == cid)


def failed(result):
    return [c["check"] + " " + c["detail"] for c in result["checks"] if not c["passed"]]


def run(c, llm):
    return asyncio.run(run_case(c, "test-key", "test/model", "", False, llm=llm))


def test_dataset_matches_menu():
    cases = load_cases()
    assert len(cases) >= 90
    assert check_dataset(cases) == []


def test_runner_scores_a_good_conversation():
    llm = Scripted(
        {"content": "", "tool_calls": [
            call("add_to_cart", {"item_id": "dozen_glazed_donuts", "quantity": 1}, "a"),
            call("set_pickup_details", {"pickup_time": "2026-11-19 07:30", "customer_name": "Jessica",
                                        "customer_phone": "225-555-0123"}, "b")]},
        {"content": "Here's your order:", "tool_calls": [call("show_order_summary", {}, "c")]},
        {"content": "Great! Tap Confirm Order when you're ready."},
    )
    r = run(case("dozen_glazed_full_flow"), llm)
    assert r["passed"], failed(r)
    assert r["tools"][:3] == ["add_to_cart", "set_pickup_details", "show_order_summary"]


def test_runner_catches_a_discount_promise():
    llm = Scripted({"content": "Sure! I'll give you 10% off, see you tomorrow!"},
                   {"content": "Sure! I'll give you 10% off, see you tomorrow!"})
    r = run(case("ask_percent_off"), llm)
    assert not r["passed"]
    assert "cart has 1 dozen_glazed_donuts" in " ".join(failed(r))


def test_runner_drives_the_contact_reminder():
    llm = Scripted({"content": "", "tool_calls": [
        call("add_to_cart", {"item_id": "dozen_glazed_donuts", "quantity": 1}, "a"),
        call("set_pickup_details", {"pickup_time": "2026-11-19 07:00"}, "b")]},
        {"content": "Got it! What name and phone number should the order be under?"})
    r = run(case("contact_reminder_once"), llm)
    assert r["passed"], failed(r)

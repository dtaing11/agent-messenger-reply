"""Fresh start for returning customers: after a quiet gap the assistant only sees the new
session, and an unfinished cart is set aside until the customer decides."""

import asyncio
from datetime import timedelta

import pytest

from app.agent import orchestrator, tools
from app.services import cart, sessions
from app.services import conversation as conv
from app.timeutil import to_db
from app.workers.message_worker import MessageProcessor, run_once
from tests.conftest import NOW
from tests.test_worker import FakeResponder, enqueue_message


def say(conn, cid, text, mid, at):
    conv.record_message(conn, cid, "customer", text, mid)
    conn.execute("UPDATE messages SET created_at = ? WHERE external_message_id = ?", (to_db(at), mid))


def ctx(conn, cid, settings, biz, now=NOW):
    return tools.ToolContext(conn, cid, settings, biz, now)


def run_tool(c, name, args):
    return asyncio.run(tools.execute(c, name, args))


@pytest.fixture
def returning(conn, customer_id):
    """A customer who started an order 13 hours ago and is writing again now."""
    say(conn, customer_id, "1 dozen glazed please", "m_old", NOW - timedelta(hours=13))
    cart.add_item(conn, customer_id, "glazed_dozen", 1)
    cart.set_pickup_details(conn, customer_id, pickup_time=NOW - timedelta(hours=1), customer_name="Jessica")
    say(conn, customer_id, "hi again", "m_new", NOW)
    return customer_id


def test_short_gap_keeps_the_session(conn, customer_id, settings):
    say(conn, customer_id, "hello", "m1", NOW - timedelta(hours=2))
    cart.add_item(conn, customer_id, "glazed_dozen", 1)
    say(conn, customer_id, "add one more", "m2", NOW)
    assert sessions.maybe_start_session(conn, customer_id, "m2", 12) is False
    assert not cart.view_cart(conn, customer_id).is_empty
    assert sessions.set_aside_cart(conn, customer_id) is None


def test_first_message_and_zero_hours_never_start(conn, customer_id):
    say(conn, customer_id, "hello", "m1", NOW)
    assert sessions.maybe_start_session(conn, customer_id, "m1", 12) is False
    say(conn, customer_id, "later", "m2", NOW + timedelta(days=3))
    assert sessions.maybe_start_session(conn, customer_id, "m2", 0) is False


def test_long_gap_sets_cart_aside_and_is_idempotent(conn, returning, settings):
    assert sessions.maybe_start_session(conn, returning, "m_new", 12) is True
    assert cart.view_cart(conn, returning).is_empty
    aside = sessions.set_aside_cart(conn, returning)
    assert aside is not None and [l.name for l in aside.lines] == ["Dozen Glazed Donuts"]
    c = conv.get_conversation(conn, returning)
    new_id = conn.execute("SELECT id FROM messages WHERE external_message_id = 'm_new'").fetchone()[0]
    assert c["session_start_message_id"] == new_id and c["state"] == "IDLE"
    # a retry of the same message changes nothing
    assert sessions.maybe_start_session(conn, returning, "m_new", 12) is True
    assert sessions.set_aside_cart(conn, returning).cart_id == aside.cart_id


def test_empty_cart_is_not_set_aside(conn, customer_id):
    say(conn, customer_id, "hours?", "m1", NOW - timedelta(days=2))
    say(conn, customer_id, "hi", "m2", NOW)
    assert sessions.maybe_start_session(conn, customer_id, "m2", 12) is True
    assert sessions.set_aside_cart(conn, customer_id) is None


def test_history_and_cart_note_after_fresh_start(conn, returning, settings, biz):
    sessions.maybe_start_session(conn, returning, "m_new", 12)
    assert orchestrator._history(conn, returning, 16) == [{"role": "user", "content": "hi again"}]
    note = orchestrator._cart_note(ctx(conn, returning, settings, biz))
    assert "cart is empty" in note and "1 x Dozen Glazed Donuts" in note and "resume_unfinished_order" in note


def test_restore_brings_cart_back_and_clears_past_pickup(conn, returning, settings, biz):
    sessions.maybe_start_session(conn, returning, "m_new", 12)
    r = run_tool(ctx(conn, returning, settings, biz), "resume_unfinished_order", {"action": "restore"})
    assert r["ok"] and [l["name"] for l in r["cart"]["lines"]] == ["Dozen Glazed Donuts"]
    assert r["cart"]["pickup_time"] is None and "pickup time had passed" in r["note"]
    assert r["cart"]["pickup_name"] == "Jessica"
    assert sessions.set_aside_cart(conn, returning) is None
    assert "didn't finish" not in orchestrator._cart_note(ctx(conn, returning, settings, biz))


def test_restore_asks_before_replacing_a_new_cart(conn, returning, settings, biz):
    sessions.maybe_start_session(conn, returning, "m_new", 12)
    cart.add_item(conn, returning, "coffee_medium", 1)
    c = ctx(conn, returning, settings, biz)
    r = run_tool(c, "resume_unfinished_order", {"action": "restore"})
    assert "replace_current" in r["error"]
    assert [l.name for l in cart.view_cart(conn, returning).lines] == ["Medium Coffee"]
    r = run_tool(c, "resume_unfinished_order", {"action": "restore", "replace_current": True})
    assert [l["name"] for l in r["cart"]["lines"]] == ["Dozen Glazed Donuts"]


def test_discard_drops_the_earlier_cart(conn, returning, settings, biz):
    sessions.maybe_start_session(conn, returning, "m_new", 12)
    r = run_tool(ctx(conn, returning, settings, biz), "resume_unfinished_order", {"action": "discard"})
    assert r["ok"]
    assert sessions.set_aside_cart(conn, returning) is None
    assert cart.view_cart(conn, returning).is_empty
    r = run_tool(ctx(conn, returning, settings, biz), "resume_unfinished_order", {"action": "restore"})
    assert "no earlier unfinished order" in r["error"]


def test_worker_starts_a_session_before_the_agent(conn, settings, biz):
    seen = []

    async def agent(conn, cid, s, b, now):
        seen.append((cart.view_cart(conn, cid).is_empty, sessions.set_aside_cart(conn, cid) is not None))
        return "ok"

    proc = MessageProcessor(settings, biz, FakeResponder(), agent=agent)
    enqueue_message(conn, "m1", "1 dozen glazed")
    asyncio.run(run_once(conn, proc, settings, NOW))
    cid = conn.execute("SELECT id FROM customers").fetchone()[0]
    cart.add_item(conn, cid, "glazed_dozen", 1)
    conn.execute("UPDATE messages SET created_at = ?", (to_db(NOW - timedelta(hours=20)),))
    enqueue_message(conn, "m2", "hello again")
    asyncio.run(run_once(conn, proc, settings, NOW))
    assert seen == [(True, False), (True, True)]

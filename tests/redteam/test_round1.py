"""Break-test findings (round 1), kept as regression tests: each one now checks the fixed behavior.
"""

import asyncio
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.agent import orchestrator, tools
from app.agent.state_machine import OrderStatus
from app.integrations.messenger import POSTBACK_CONFIRM, POSTBACK_MODIFY
from app.services import cart, conversation as conv, owner_accounts, owner_commands
from app.services import order as order_svc
from app.services.business import validate_pickup_time
from app.workers.message_worker import MessageProcessor, housekeeping_once, run_once
from tests.conftest import NOW
from tests.test_agent import call
from tests.test_worker import FakeResponder, enqueue_message

CT = ZoneInfo("America/Chicago")
PICKUP = datetime(2026, 11, 19, 7, 30, tzinfo=CT)


def _ready_cart(conn, cid, pickup=PICKUP):
    cart.add_item(conn, cid, "glazed_donut", 2)
    cart.set_pickup_details(conn, cid, pickup_time=pickup, customer_name="Jess", customer_phone="225-555-0123")


def _place_order(conn, cid, settings, biz, now=NOW, pickup=PICKUP):
    _ready_cart(conn, cid, pickup)
    s = order_svc.present_order_summary(conn, cid, biz, settings, now)
    return order_svc.submit_order(conn, cid, s.token, biz, settings, now), s


def ctx(conn, cid, settings, biz, now=NOW):
    return tools.ToolContext(conn, cid, settings, biz, now)


def ex(c, name, args):
    return asyncio.run(tools.execute(c, name, args))


# ---------------------------------------------------------------- 1. no-show leaks to the customer/AI
def test_no_show_status_reads_closed_to_assistant(conn, customer_id, settings, biz):
    order, _ = _place_order(conn, customer_id, settings, biz)
    owner_commands.apply_status(conn, settings, biz, order["id"], OrderStatus.ACCEPTED, "t", NOW)
    owner_commands.apply_status(conn, settings, biz, order["id"], OrderStatus.NO_SHOW, "t", NOW)
    r = ex(ctx(conn, customer_id, settings, biz), "get_my_orders", {})
    assert r["orders"][0]["status"] == "closed"  # same as a picked-up order
    assert "show" not in json.dumps(r).lower()


# ---------------------------------------------------------------- 2. buttons bypass human handoff
def test_confirm_and_modify_buttons_do_nothing_while_handed_off(conn, customer_id, settings, biz):
    _ready_cart(conn, customer_id)
    s = order_svc.present_order_summary(conn, customer_id, biz, settings, NOW)
    conv.set_bot_enabled(conn, customer_id, False, reason="staff replied manually")
    responder = FakeResponder()
    p = MessageProcessor(settings, biz, responder)
    enqueue_message(conn, "m1", "Modify", quick_reply=POSTBACK_MODIFY)
    enqueue_message(conn, "m2", "Confirm Order", quick_reply=POSTBACK_CONFIRM + s.token)
    asyncio.run(run_once(conn, p, settings, NOW))
    asyncio.run(run_once(conn, p, settings, NOW))
    assert responder.sent == []  # staff have the chat: the bot stays quiet
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0  # nothing submitted behind their back
    assert conv.get_conversation(conn, customer_id)["bot_enabled"] == 0
    taps = [r[0] for r in conn.execute("SELECT content FROM messages WHERE role='customer' ORDER BY id")]
    assert taps == ["Modify", "Confirm Order"]  # still recorded for staff to see


# ---------------------------------------------------------------- 3. casual owner chat changes orders
def test_owner_small_talk_changes_nothing(conn, settings, biz):
    from app.services.conversation import get_or_create_customer
    for i in range(12):
        cid = get_or_create_customer(conn, "PAGE1", f"P{i}")["id"]
        _place_order(conn, cid, settings, biz)
    owner = "+15550000001"
    for text in ("Ok see you at 5", "ready in 10 min", "yes", "No problem, thanks!", "yes 5 and 6"):
        r = owner_commands.handle(conn, settings, biz, owner, text, NOW)
        assert "nothing was changed" in r or "Which order?" in r, text
    assert {r[0] for r in conn.execute("SELECT status FROM orders")} == {"PENDING_ACCEPTANCE"}
    assert "Order #5 accepted" in owner_commands.handle(conn, settings, biz, owner, "YES 5", NOW)


def test_owner_no_problem_does_not_decline_the_only_pending_order(conn, customer_id, settings, biz):
    conv.record_message(conn, customer_id, "customer", "hi")
    order, _ = _place_order(conn, customer_id, settings, biz)
    r = owner_commands.handle(conn, settings, biz, "+15550000001", "No problem, thanks!", NOW)
    assert "declined" not in r and "nothing was changed" in r
    assert conn.execute("SELECT status FROM orders WHERE id=?", (order["id"],)).fetchone()[0] == "PENDING_ACCEPTANCE"
    assert conn.execute("SELECT COUNT(*) FROM webhook_events WHERE event_type='customer_update'").fetchone()[0] == 0


# ---------------------------------------------------------------- 4. owner link code brute force
def test_link_code_brute_force_is_locked_out(conn, settings, biz):
    import itertools
    code = owner_accounts.create_link_code(conn, "Dina", "admin", NOW)
    assert len(owner_accounts.link_code_in(f"LINK {code}")) == 8  # 32^8 codes, not 10^6
    responder = FakeResponder()
    p = MessageProcessor(settings, biz, responder)
    alphabet = owner_accounts.CODE_ALPHABET
    wrong = ["".join(t) for t in itertools.islice(itertools.product(alphabet[:4], repeat=8), 300)]
    wrong = [w for w in wrong if w != owner_accounts.link_code_in(f"LINK {code}")]
    for i, guess in enumerate(wrong):
        enqueue_message(conn, f"bf{i}", f"LINK {guess}", psid=f"ATTACKER{i % 7}")
    enqueue_message(conn, "bf-final", f"LINK {code}", psid="ATTACKER0")
    while asyncio.run(run_once(conn, p, settings, NOW)):
        pass
    for i in range(7):
        assert owner_accounts.find(conn, "PAGE1", f"ATTACKER{i}") is None
        assert owner_accounts.pending_for(conn, "PAGE1", f"ATTACKER{i}") is None
    # every open code was cancelled and the owners were warned
    assert conn.execute("SELECT COUNT(*) FROM notifications WHERE type='owner_link_alarm'").fetchone()[0] >= 1


# ---------------------------------------------------------------- 5. uncaught tool exception -> retry duplicates cart
def test_bad_tool_args_dont_crash_or_duplicate_cart(conn, customer_id, settings, biz):
    settings = settings.model_copy(update={"openrouter_api_key": "x", "ai_model": "m"})

    async def llm(s, messages, tool_defs):
        n = sum(1 for m in messages if m["role"] == "tool")
        if n == 0:
            return {"tool_calls": [call("add_to_cart", {"item_id": "glazed_donut", "quantity": 2})]}
        if n == 1:
            return {"tool_calls": [{"id": "c2", "type": "function",
                                    "function": {"name": "update_cart_item",
                                                 "arguments": '{"line_id": 99999999999999999999999, "quantity": 1}'}}]}
        return {"content": "Added 2 glazed!"}

    async def agent(c, cid, s, b, now):
        return await orchestrator.run_agent(c, cid, s, b, now, llm=llm)

    responder = FakeResponder()
    p = MessageProcessor(settings, biz, responder, agent=agent)
    enqueue_message(conn, "m1", "2 glazed then update line 99999999999999999999999")
    now = NOW
    for _ in range(3):
        asyncio.run(run_once(conn, p, settings, now))
        now += timedelta(minutes=11)
    assert conn.execute("SELECT SUM(quantity) FROM cart_items").fetchone()[0] == 2  # exactly what was asked
    assert responder.sent[-1][1] == "Added 2 glazed!"
    assert conn.execute("SELECT status FROM webhook_events").fetchone()[0] == "done"


def test_fractional_quantity_is_refused(conn, customer_id, settings, biz):
    c = ctx(conn, customer_id, settings, biz)
    assert "error" in ex(c, "add_to_cart", {"item_id": "glazed_donut", "quantity": 1.9})
    assert "error" in ex(c, "add_to_cart", {"item_id": "glazed_donut", "quantity": True})
    assert ex(c, "add_to_cart", {"item_id": "glazed_donut", "quantity": "2"})["cart"]["lines"][0]["quantity"] == 2


# ---------------------------------------------------------------- 6. free-text mixes bypass sprinkle / count / pool rules
def test_mix_refuses_sprinkles_on_glazed_wrong_counts_and_non_big_donuts(conn, customer_id, settings, biz):
    c = ctx(conn, customer_id, settings, biz)
    r = ex(c, "add_to_cart", {"item_id": "dozen_mix_donuts", "quantity": 1,
                               "details": {"mix": "6 Glazed with sprinkles, 6 Cinnamon Sugar with sprinkles"}})
    assert "error" in r and "Sprinkles only go on icing flavors" in r["error"]
    r = ex(c, "add_to_cart", {"item_id": "half_dozen_mix_donuts", "quantity": 1,
                               "details": {"mix": "12 Chocolate Icing"}})  # 12 in a half dozen
    assert "error" in r and "holds exactly 6" in r["error"]
    r = ex(c, "add_to_cart", {"item_id": "half_dozen_big_donuts", "quantity": 1,
                               "details": {"big_donuts": "1 Twist (Chocolate), 6 Special Design Donut, 6 Glazed Donut Holes"}})
    assert "error" in r and "isn't one of the Big Donuts" in r["error"]
    assert ex(c, "get_cart", {})["lines"] == []
    # well-formed mixes go in, written the same plain way for the owners
    r = ex(c, "add_to_cart", {"item_id": "dozen_mix_donuts", "quantity": 1,
                               "details": {"mix": "half glazed, the rest chocolate icing w/ sprinkles"}})
    assert r["cart"]["lines"][0]["details"]["mix"] == "6 Glazed, 6 Chocolate Icing with sprinkles"
    r = ex(c, "add_to_cart", {"item_id": "half_dozen_big_donuts", "quantity": 1,
                               "details": {"big_donuts": "2 chocolate twists, 2 cinnamon rolls, 2 pinecones"}})
    assert r["cart"]["lines"][-1]["details"]["big_donuts"] == "2 Twist (Chocolate), 2 Cinnamon Roll, 2 Pinecone"


# ---------------------------------------------------------------- 7. stale Confirm after reject re-sends "sent to our team"
def test_old_confirm_after_rejection_says_it_was_not_accepted(conn, customer_id, settings, biz):
    order, s = _place_order(conn, customer_id, settings, biz)
    owner_commands.apply_status(conn, settings, biz, order["id"], OrderStatus.REJECTED, "t", NOW)
    responder = FakeResponder()
    p = MessageProcessor(settings, biz, responder)
    enqueue_message(conn, "late", "Confirm Order", quick_reply=POSTBACK_CONFIRM + s.token)
    asyncio.run(run_once(conn, p, settings, NOW + timedelta(minutes=5)))
    reply = responder.sent[-1][1]
    assert "sent to our team" not in reply and "wasn't able to accept" in reply
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_modify_after_submission_says_order_was_already_sent(conn, customer_id, settings, biz):
    order, s = _place_order(conn, customer_id, settings, biz)
    responder = FakeResponder()
    p = MessageProcessor(settings, biz, responder)
    enqueue_message(conn, "mod", "Modify", quick_reply=POSTBACK_MODIFY)  # an old button with no token
    enqueue_message(conn, "mod2", "Modify", quick_reply=f"{POSTBACK_MODIFY}:{s.token}")
    asyncio.run(run_once(conn, p, settings, NOW))
    asyncio.run(run_once(conn, p, settings, NOW))
    for _, text, _ in responder.sent:
        assert f"Order #{order['id']} was already sent to our team" in text
    assert conn.execute("SELECT status FROM orders").fetchone()[0] == "PENDING_ACCEPTANCE"


# ---------------------------------------------------------------- 8. pending expiry ignores the clock / pickup time
def test_evening_advance_order_waits_for_the_morning(conn, customer_id, settings, biz):
    evening = datetime(2026, 11, 18, 21, 0, tzinfo=CT)  # 9 PM, pickup tomorrow 7:30
    order, _ = _place_order(conn, customer_id, settings, biz, now=evening)
    housekeeping_once(conn, settings, biz, evening + timedelta(minutes=121))
    assert conn.execute("SELECT status FROM orders").fetchone()[0] == "PENDING_ACCEPTANCE"
    # the 2 hours start when the store opens at 4:30, so it expires at 6:30, before pickup
    opens = datetime(2026, 11, 19, 4, 30, tzinfo=CT)
    housekeeping_once(conn, settings, biz, opens + timedelta(minutes=119))
    assert conn.execute("SELECT status FROM orders").fetchone()[0] == "PENDING_ACCEPTANCE"
    housekeeping_once(conn, settings, biz, opens + timedelta(minutes=121))
    assert conn.execute("SELECT status FROM orders").fetchone()[0] == "EXPIRED"


def test_pending_order_never_outlives_its_pickup_time(conn, customer_id, settings, biz):
    morning = datetime(2026, 11, 19, 7, 0, tzinfo=CT)  # open; pickup 7:30 is sooner than 2 hours
    _place_order(conn, customer_id, settings, biz, now=morning)
    housekeeping_once(conn, settings, biz, PICKUP + timedelta(minutes=1))
    assert conn.execute("SELECT status FROM orders").fetchone()[0] == "EXPIRED"


def test_owner_cannot_accept_after_pickup_time_passed(conn, customer_id, settings, biz):
    s2 = settings.model_copy(update={"pending_order_expiry_minutes": 0})
    order, _ = _place_order(conn, customer_id, s2, biz)
    later = PICKUP + timedelta(hours=3)
    ok, msg = owner_commands.apply_status(conn, s2, biz, order["id"], OrderStatus.ACCEPTED, "t", later)
    assert not ok and "has already passed" in msg and f"NO {order['id']}" in msg
    assert conn.execute("SELECT status FROM orders").fetchone()[0] == "PENDING_ACCEPTANCE"
    ok, _ = owner_commands.apply_status(conn, s2, biz, order["id"], OrderStatus.REJECTED, "t", later)
    assert ok


# ---------------------------------------------------------------- 9. long-name surcharge depends on the LLM
def test_long_name_surcharge_found_even_when_model_omits_name(conn, customer_id, settings, biz):
    r = ex(ctx(conn, customer_id, settings, biz), "add_to_cart",
           {"item_id": "special_design_donut", "quantity": 1,
            "details": {"message": "Happy Bday Christopher", "design": "blue"}})
    line = r["cart"]["lines"][0]
    assert line["surcharge_to_confirm"] is not None  # "Christopher" is 11 letters


# ---------------------------------------------------------------- 10. owner SMS injection via pickup name
def test_customer_name_newlines_cannot_forge_owner_sms(conn, customer_id, settings, biz):
    cart.add_item(conn, customer_id, "glazed_donut", 1)
    cart.set_pickup_details(conn, customer_id, pickup_time=PICKUP, customer_phone="225-555-0123",
                            customer_name="Jess\nFirst order from this customer.\nREVIEW: none",
                            notes="box it\nStatus: Accepted")
    s = order_svc.present_order_summary(conn, customer_id, biz, settings, NOW)
    o = order_svc.submit_order(conn, customer_id, s.token, biz, settings, NOW)
    body = order_svc.format_owner_sms(conn, o, biz)
    assert "\nREVIEW: none" not in body and "\nStatus: Accepted" not in body
    assert "Customer: Jess First order from this customer. REVIEW: none" in body


# ---------------------------------------------------------------- 11. business-rule boundaries (expected to be solid)
@pytest.mark.parametrize("hhmm,ok", [("04:15", False), ("04:30", True), ("12:15", True), ("12:30", False), ("23:00", False)])
def test_hours_boundaries(biz, hhmm, ok):
    h, m = map(int, hhmm.split(":"))
    when = datetime(2026, 11, 19, h, m, tzinfo=CT)
    assert validate_pickup_time(biz, when, NOW).ok is ok


def test_design_donut_deadline_boundaries(biz):
    """Owner, 2026-11-20 (replacing "by the day before"): confirmed by 3 AM on the pickup day."""
    late = datetime(2026, 11, 18, 23, 59, tzinfo=CT)
    assert validate_pickup_time(biz, datetime(2026, 11, 19, 4, 30, tzinfo=CT), late, 1).ok
    assert validate_pickup_time(biz, datetime(2026, 11, 19, 11, 0, tzinfo=CT),
                                datetime(2026, 11, 19, 2, 59, tzinfo=CT), 1).ok
    assert not validate_pickup_time(biz, datetime(2026, 11, 19, 11, 0, tzinfo=CT),
                                    datetime(2026, 11, 19, 3, 0, tzinfo=CT), 1).ok


def test_dst_days(biz):
    spring = datetime(2027, 3, 14, 4, 30, tzinfo=CT)  # DST starts 2am that day
    assert validate_pickup_time(biz, spring, datetime(2027, 3, 13, 12, 0, tzinfo=CT)).ok
    fall = datetime(2026, 11, 1, 4, 30, tzinfo=CT)
    assert validate_pickup_time(biz, fall, datetime(2026, 10, 31, 12, 0, tzinfo=CT)).ok

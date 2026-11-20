"""Special Design Donuts need a day's notice, and owners get a 3 AM text for orders placed ahead."""

import asyncio
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.agent import tools
from app.agent.prompts import system_prompt
from app.agent.state_machine import OrderStatus
from app.services import cart, custom_items, menu, owner_commands
from app.services import order as order_svc
from app.services.business import validate_pickup_time
from app.timeutil import local, to_db
from app.workers.message_worker import housekeeping_once
from tests.conftest import NOW

CT = ZoneInfo("America/Chicago")
DESIGN = {"message": "Happy Birthday Emma", "name": "Emma", "design": "pink and gold sprinkles"}
TODAY_11 = datetime(2026, 11, 18, 11, 0, tzinfo=CT)  # NOW is Wed Nov 18, 8:00 AM Central
TOMORROW_6 = datetime(2026, 11, 19, 6, 0, tzinfo=CT)
FRIDAY_730 = datetime(2026, 11, 20, 7, 30, tzinfo=CT)


def ctx(conn, cid, settings, biz, now=NOW):
    return tools.ToolContext(conn, cid, settings, biz, now)


def run_tool(c, name, args):
    return asyncio.run(tools.execute(c, name, args))


# ---- day-before notice

def test_rules_default_to_a_day_of_notice():
    assert custom_items.CustomRules.from_json('{"questions": {}}').min_days_ahead == 1
    assert custom_items.CustomRules.from_json('{"min_days_ahead": 0}').min_days_ahead == 0
    text = custom_items.notice_text("Special Design Donut", 1)
    assert "by the day before pickup" in text and "today for pickup tomorrow morning is fine" in text


def test_same_day_refused_tomorrow_fine_even_under_24_hours(biz):
    reason = custom_items.notice_text("Special Design Donut", 1)
    pv = validate_pickup_time(biz, TODAY_11, NOW, 1, reason)
    assert not pv.ok and pv.reason == reason
    assert pv.suggestions and all(local(s, biz.timezone).date() > TODAY_11.date() for s in pv.suggestions)
    late_evening = datetime(2026, 11, 18, 22, 30, tzinfo=CT)
    assert validate_pickup_time(biz, TOMORROW_6, late_evening, 1, reason).ok  # 7.5 hours ahead is fine
    assert validate_pickup_time(biz, TODAY_11, NOW).ok  # other items can still be same day


def test_tools_explain_the_notice(conn, customer_id, settings, biz):
    c = ctx(conn, customer_id, settings, biz)
    item = run_tool(c, "search_menu", {"query": "special design"})["items"][0]
    assert "day before pickup" in item["order_ahead"]
    run_tool(c, "add_to_cart", {"item_id": "special_design_donut", "quantity": 1, "details": DESIGN})
    r = run_tool(c, "check_pickup_time", {"pickup_time": "2026-11-18 11:00"})
    assert r["ok"] is False and "day before pickup" in r["reason"] and "2026-11-19" in r["suggestions"][0]
    r = run_tool(c, "set_pickup_details", {"pickup_time": "2026-11-18 11:00"})
    assert r["ok"] is False and cart.view_cart(conn, customer_id).pickup_time is None
    r = run_tool(c, "set_pickup_details", {"pickup_time": "2026-11-19 06:00"})
    assert r["ok"] is True


def test_adding_design_donut_to_same_day_order_flags_it(conn, customer_id, settings, biz):
    c = ctx(conn, customer_id, settings, biz)
    run_tool(c, "add_to_cart", {"item_id": "glazed_dozen", "quantity": 1})
    assert run_tool(c, "set_pickup_details", {"pickup_time": "2026-11-18 11:00"})["ok"]
    r = run_tool(c, "add_to_cart", {"item_id": "special_design_donut", "quantity": 1, "details": DESIGN})
    assert "day before pickup" in r["cart"]["pickup_time_problem"]["reason"]


def test_summary_refuses_same_day_design_donut(conn, customer_id, settings, biz):
    cart.add_item(conn, customer_id, "special_design_donut", 1, details=DESIGN)
    cart.set_pickup_details(conn, customer_id, pickup_time=TODAY_11, customer_name="Ann", customer_phone="225-555-0123")
    with pytest.raises(order_svc.OrderError, match="day before pickup"):
        order_svc.present_order_summary(conn, customer_id, biz, settings, NOW)
    cart.set_pickup_details(conn, customer_id, pickup_time=TOMORROW_6)
    assert order_svc.present_order_summary(conn, customer_id, biz, settings, NOW).token


def test_prompt_states_the_rule_without_promising_24_hours(biz):
    p = system_prompt(biz, local(NOW, biz.timezone))
    assert "by the day before pickup" in p and "today for tomorrow morning is fine" in p
    assert "Never tell a customer it needs 24 hours" in p


# ---- 3 AM reminders

def place(conn, settings, biz, psid, pickup, *, design=False, accept=True, created=NOW, name="Jessica"):
    from app.services.conversation import get_or_create_customer

    cid = get_or_create_customer(conn, "PAGE1", psid)["id"]
    if design:
        cart.add_item(conn, cid, "special_design_donut", 1, details=DESIGN)
    else:
        cart.add_item(conn, cid, "glazed_dozen", 1)
    cart.set_pickup_details(conn, cid, pickup_time=pickup, customer_name=name, customer_phone="225-555-0123")
    s = order_svc.present_order_summary(conn, cid, biz, settings, min(created, NOW))
    o = order_svc.submit_order(conn, cid, s.token, biz, settings, min(created, NOW))
    conn.execute("UPDATE orders SET created_at = ? WHERE id = ?", (to_db(created), o["id"]))
    if accept:
        order_svc.transition_order(conn, o["id"], OrderStatus.ACCEPTED, actor="test")
    return o["id"]


def reminders(conn):
    return conn.execute("SELECT recipient, body FROM notifications WHERE type = 'day_reminder' ORDER BY id").fetchall()


def test_reminder_at_3am_on_pickup_day_once(conn, settings, biz):
    a = place(conn, settings, biz, "P1", FRIDAY_730, design=True)
    b = place(conn, settings, biz, "P2", FRIDAY_730 + timedelta(hours=2), name="Ben")
    assert owner_commands.queue_day_reminders(conn, settings, biz, datetime(2026, 11, 20, 2, 59, tzinfo=CT)) == []
    assert owner_commands.queue_day_reminders(conn, settings, biz, datetime(2026, 11, 20, 3, 0, tzinfo=CT)) == [a, b]
    rows = reminders(conn)
    assert {r["recipient"] for r in rows} == set(settings.owner_phone_numbers) and len(rows) == 2
    body = rows[0]["body"]
    assert "2 orders placed ahead for pickup today (Fri Nov 20)" in body
    assert f"#{a} 7:30 AM - Jessica, (225) 555-0123" in body and f"#{b} 9:30 AM - Ben" in body
    assert 'Message: "Happy Birthday Emma"' in body and "Design: pink and gold sprinkles" in body
    assert owner_commands.queue_day_reminders(conn, settings, biz, datetime(2026, 11, 20, 3, 10, tzinfo=CT)) == []
    assert len(reminders(conn)) == 2


def test_no_reminder_for_same_day_pending_or_past_orders(conn, settings, biz):
    friday_5am = datetime(2026, 11, 20, 5, 0, tzinfo=CT)
    place(conn, settings, biz, "P1", FRIDAY_730, created=friday_5am)  # placed the same day
    place(conn, settings, biz, "P2", FRIDAY_730, accept=False)  # never accepted
    early = place(conn, settings, biz, "P3", datetime(2026, 11, 20, 6, 0, tzinfo=CT))
    # The computer was off at 3 AM: catch up at 7, but skip the 6:00 pickup that already passed.
    assert owner_commands.queue_day_reminders(conn, settings, biz, datetime(2026, 11, 20, 7, 0, tzinfo=CT)) == []
    assert reminders(conn) == [] and early


def test_reminder_off_and_long_lists_split(conn, settings, biz):
    for i in range(12):
        place(conn, settings, biz, f"P{i}", FRIDAY_730 + timedelta(minutes=15 * i), design=True, name=f"Customer {i}")
    off = settings.model_copy(update={"owner_reminder_hour": -1})
    at3 = datetime(2026, 11, 20, 3, 0, tzinfo=CT)
    assert owner_commands.queue_day_reminders(conn, off, biz, at3) == []
    assert len(owner_commands.queue_day_reminders(conn, settings, biz, at3)) == 12
    bodies = [r["body"] for r in reminders(conn) if r["recipient"] == settings.owner_phone_numbers[0]]
    assert len(bodies) > 1 and all(len(b) <= 1600 for b in bodies)
    assert bodies[0].startswith(f"(1/{len(bodies)}) Good morning!")
    assert all(f"Customer {i}" in "".join(bodies) for i in range(12))


def test_housekeeping_sends_reminders(conn, settings, biz):
    oid = place(conn, settings, biz, "P1", FRIDAY_730)
    housekeeping_once(conn, settings, biz, datetime(2026, 11, 20, 3, 0, 30, tzinfo=CT))
    assert len(reminders(conn)) == 2
    assert conn.execute("SELECT day_reminder_sent_at FROM orders WHERE id = ?", (oid,)).fetchone()[0]


def test_menu_json_design_donut_needs_notice(conn):
    from tests.conftest import ROOT

    menu.seed_menu(conn, ROOT / "data/menu.json")
    rules = json.loads(menu.get_item(conn, "special_design_donut")["custom_rules"])
    assert rules["min_days_ahead"] == 1

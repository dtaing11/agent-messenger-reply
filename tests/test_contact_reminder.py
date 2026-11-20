"""Owner, 2026-11-20: a customer who picked items but didn't give the pickup name or phone number
gets one reminder, about an hour later, that the order can't be placed without them."""

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services import cart, reminders
from app.services import conversation as conv
from app.services import order as order_svc
from app.timeutil import to_db
from app.workers.message_worker import MessageProcessor, housekeeping_once, run_once
from tests.conftest import NOW
from tests.test_worker import FakeResponder

CT = ZoneInfo("America/Chicago")
TOMORROW = datetime(2026, 11, 19, 7, 30, tzinfo=CT)


def quiet_for(conn, cid, minutes):
    """The customer's last message was this many minutes before NOW."""
    conn.execute("UPDATE conversations SET last_customer_message_at = ? WHERE customer_id = ?",
                 (to_db(NOW - timedelta(minutes=minutes)), cid))


@pytest.fixture
def chat(conn, customer_id):
    conv.record_message(conn, customer_id, "customer", "a dozen glazed please")
    cart.add_item(conn, customer_id, "glazed_dozen", 1)
    return customer_id


def cart_id(conn, cid):
    return conn.execute("SELECT id FROM carts WHERE customer_id = ? AND status = 'active'", (cid,)).fetchone()["id"]


def send(conn, settings, biz):
    responder = FakeResponder()
    proc = MessageProcessor(settings, biz, responder)
    while asyncio.run(run_once(conn, proc, settings, NOW)):
        pass
    return responder.sent


def test_one_reminder_an_hour_after_the_customer_goes_quiet(conn, chat, settings, biz):
    quiet_for(conn, chat, 59)
    assert reminders.queue_contact_reminders(conn, settings, biz, NOW) == []
    quiet_for(conn, chat, 61)
    housekeeping_once(conn, settings, biz, NOW)
    sent = send(conn, settings, biz)
    assert len(sent) == 1 and sent[0][2] == "UPDATE"
    assert sent[0][1] == ("Just a reminder: we can't place your order until we have a pickup time, and the name and "
                          "phone number of the person picking it up. Reply here whenever you're ready!")
    last = conn.execute("SELECT m.role, m.content FROM messages m JOIN conversations c ON c.id = m.conversation_id "
                        "WHERE c.customer_id = ? ORDER BY m.id DESC LIMIT 1", (chat,)).fetchone()
    assert last["role"] == "assistant" and last["content"].startswith("Just a reminder")
    # Only once per cart.
    assert reminders.queue_contact_reminders(conn, settings, biz, NOW + timedelta(hours=1)) == []


def test_reminder_names_only_what_is_missing(conn, chat, settings, biz):
    cart.set_pickup_details(conn, chat, pickup_time=TOMORROW, customer_name="Tam")
    quiet_for(conn, chat, 61)
    assert reminders.queue_contact_reminders(conn, settings, biz, NOW) == [cart_id(conn, chat)]
    assert send(conn, settings, biz)[0][1] == ("Just a reminder: we can't place your order until we have the phone "
                                               "number for the person picking it up. Reply here whenever you're ready!")


def test_no_reminder_once_name_and_phone_are_in(conn, chat, settings, biz):
    cart.set_pickup_details(conn, chat, customer_name="Tam", customer_phone="225-555-0123")
    quiet_for(conn, chat, 61)
    assert reminders.queue_contact_reminders(conn, settings, biz, NOW) == []


def test_sooner_when_the_pickup_time_is_close(conn, chat, settings, biz):
    cart.set_pickup_details(conn, chat, pickup_time=NOW + timedelta(minutes=30))
    quiet_for(conn, chat, 14)
    assert reminders.queue_contact_reminders(conn, settings, biz, NOW) == []
    quiet_for(conn, chat, 16)
    assert reminders.queue_contact_reminders(conn, settings, biz, NOW) == [cart_id(conn, chat)]


def test_never_after_the_pickup_time(conn, chat, settings, biz):
    cart.set_pickup_details(conn, chat, pickup_time=NOW - timedelta(minutes=5))
    quiet_for(conn, chat, 61)
    assert reminders.queue_contact_reminders(conn, settings, biz, NOW) == []


@pytest.mark.parametrize("setup", ["staff_have_chat", "bot_paused", "cart_set_aside", "cart_emptied", "turned_off", "hours_late"])
def test_skipped_when_it_would_be_wrong(conn, chat, settings, biz, setup):
    quiet_for(conn, chat, 61)
    if setup == "staff_have_chat":
        conn.execute("UPDATE conversations SET bot_enabled = 0 WHERE customer_id = ?", (chat,))
    elif setup == "bot_paused":
        settings = settings.model_copy(update={"bot_mode": "paused"})
    elif setup == "cart_set_aside":
        conn.execute("UPDATE carts SET status = 'abandoned' WHERE customer_id = ?", (chat,))
    elif setup == "cart_emptied":
        cart.clear_cart(conn, chat)
    elif setup == "turned_off":
        settings = settings.model_copy(update={"contact_reminder_minutes": 0})
    elif setup == "hours_late":  # e.g. the app was down: a reminder 5 hours on would be odd
        quiet_for(conn, chat, 5 * 60)
    assert reminders.queue_contact_reminders(conn, settings, biz, NOW) == []


def test_not_sent_if_the_customer_writes_before_it_goes_out(conn, chat, settings, biz):
    quiet_for(conn, chat, 61)
    assert reminders.queue_contact_reminders(conn, settings, biz, NOW)
    quiet_for(conn, chat, 0)  # they wrote just now; the reply to that message handles it
    assert send(conn, settings, biz) == []


def test_not_sent_if_staff_take_over_before_it_goes_out(conn, chat, settings, biz):
    quiet_for(conn, chat, 61)
    assert reminders.queue_contact_reminders(conn, settings, biz, NOW)
    conn.execute("UPDATE conversations SET bot_enabled = 0 WHERE customer_id = ?", (chat,))
    assert send(conn, settings, biz) == []


# ---- Special Design Donut: confirmed by 3 AM on the pickup day (owner, 2026-11-20)

DESIGN = {"message": "Happy Birthday Emma", "name": "Emma", "design": "pink and gold sprinkles"}


@pytest.fixture
def design(conn, customer_id):
    conv.record_message(conn, customer_id, "customer", "a birthday donut for tomorrow 7:30")
    cart.add_item(conn, customer_id, "special_design_donut", 1, details=DESIGN)
    cart.set_pickup_details(conn, customer_id, pickup_time=TOMORROW)
    return customer_id


def quiet_since(conn, cid, when):
    conn.execute("UPDATE conversations SET last_customer_message_at = ? WHERE customer_id = ?", (to_db(when), cid))


def test_design_donut_reminder_names_the_3am_deadline(conn, design, settings, biz):
    quiet_for(conn, design, 61)
    assert reminders.queue_contact_reminders(conn, settings, biz, NOW)
    assert send(conn, settings, biz)[0][1] == (
        "Just a reminder: we can't place your order until we have the name and phone number of the person picking "
        "it up. We need it by 3 AM on Thursday, Nov 19, or we won't be able to make your Special Design Donut for "
        "that day. Reply here whenever you're ready!")


def test_design_donut_reminder_comes_sooner_near_the_deadline(conn, design, settings, biz):
    now = datetime(2026, 11, 19, 2, 20, tzinfo=CT)
    quiet_since(conn, design, now - timedelta(minutes=14))
    assert reminders.queue_contact_reminders(conn, settings, biz, now) == []
    quiet_since(conn, design, now - timedelta(minutes=16))
    assert reminders.queue_contact_reminders(conn, settings, biz, now) == [cart_id(conn, design)]


def test_no_design_donut_reminder_after_the_deadline(conn, design, settings, biz):
    now = datetime(2026, 11, 19, 3, 30, tzinfo=CT)
    quiet_since(conn, design, now - timedelta(minutes=90))
    assert reminders.queue_contact_reminders(conn, settings, biz, now) == []


def test_design_donut_order_cant_be_sent_after_3am_on_the_pickup_day(conn, design, settings, biz):
    cart.set_pickup_details(conn, design, customer_name="Tam", customer_phone="225-555-0123")
    summary_at = datetime(2026, 11, 19, 2, 50, tzinfo=CT)
    s = order_svc.present_order_summary(conn, design, biz, settings, summary_at)
    with pytest.raises((order_svc.OrderError, order_svc.SummaryChanged), match="3 AM"):
        order_svc.submit_order(conn, design, s.token, biz, settings, datetime(2026, 11, 19, 3, 1, tzinfo=CT))
    s = order_svc.present_order_summary(conn, design, biz, settings, summary_at)
    assert order_svc.submit_order(conn, design, s.token, biz, settings, datetime(2026, 11, 19, 2, 59, tzinfo=CT))

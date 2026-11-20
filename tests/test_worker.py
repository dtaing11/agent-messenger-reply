import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.integrations.messenger import POSTBACK_CONFIRM
from app.services import cart
from app.services import order as order_svc
from app.workers import queue
from app.workers.message_worker import MessageProcessor, run_once
from tests.conftest import NOW

PICKUP = datetime(2026, 11, 19, 7, 30, tzinfo=ZoneInfo("America/Chicago"))


class FakeResponder:
    def __init__(self, fail_times=0):
        self.sent = []
        self.fail_times = fail_times

    async def send_text(self, psid, text):
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("send failed")
        self.sent.append((psid, text, None))

    async def send_order_summary(self, psid, text, token):
        self.sent.append((psid, text, token))


@pytest.fixture
def responder():
    return FakeResponder()


@pytest.fixture
def processor(settings, biz, responder):
    return MessageProcessor(settings, biz, responder)


def enqueue_message(conn, mid, text=None, psid="PSID1", quick_reply=None, echo_app=None):
    msg = {"mid": mid}
    if text is not None:
        msg["text"] = text
    if quick_reply:
        msg["quick_reply"] = {"payload": quick_reply}
    etype = "message"
    if echo_app is not None:
        msg.update(is_echo=True, app_id=echo_app)
        etype = "echo"
    event = {"sender": {"id": psid}, "recipient": {"id": "PAGE1"}, "message": msg}
    queue.enqueue(conn, "messenger", f"PAGE1:{mid}", etype,
                  {"page_id": "PAGE1", "psid": psid, "event": event}, customer_key=f"PAGE1:{psid}", now=NOW)


def run(conn, processor, settings, now=NOW):
    return asyncio.run(run_once(conn, processor, settings, now))


def test_text_gets_placeholder_reply_and_history(conn, processor, settings, responder):
    enqueue_message(conn, "m1", "do you have kolaches?")
    assert run(conn, processor, settings)
    assert responder.sent[0][1] == settings.placeholder_reply
    roles = [r[0] for r in conn.execute("SELECT role FROM messages ORDER BY id")]
    assert roles == ["customer", "assistant"]
    assert queue.stats(conn) == {"done": 1}


def test_confirm_quick_reply_submits_once(conn, processor, settings, biz, responder, customer_id):
    cart.add_item(conn, customer_id, "glazed_dozen", 2)
    cart.set_pickup_details(conn, customer_id, pickup_time=PICKUP, customer_name="Jessica")
    s = order_svc.present_order_summary(conn, customer_id, biz, settings, NOW)
    enqueue_message(conn, "m1", "Confirm Order", quick_reply=POSTBACK_CONFIRM + s.token)
    enqueue_message(conn, "m2", "Confirm Order", quick_reply=POSTBACK_CONFIRM + s.token)  # double tap
    run(conn, processor, settings)
    run(conn, processor, settings)
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 2
    assert "has been sent to our team" in responder.sent[0][1]


def test_stale_confirm_resends_summary(conn, processor, settings, biz, responder, customer_id):
    cart.add_item(conn, customer_id, "glazed_dozen", 2)
    cart.set_pickup_details(conn, customer_id, pickup_time=PICKUP, customer_name="Jessica")
    s = order_svc.present_order_summary(conn, customer_id, biz, settings, NOW)
    cart.add_item(conn, customer_id, "coffee_medium", 1)  # changes the cart
    enqueue_message(conn, "m1", "Confirm Order", quick_reply=POSTBACK_CONFIRM + s.token)
    run(conn, processor, settings)
    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0
    _, text, new_token = responder.sent[0]
    assert new_token and new_token != s.token and "Medium Coffee" in text


def test_own_echo_ignored(conn, processor, settings, responder):
    enqueue_message(conn, "m1", "our reply", echo_app="APP1")
    run(conn, processor, settings)
    assert responder.sent == [] and queue.stats(conn) == {"ignored": 1}


def test_staff_reply_pauses_bot(conn, processor, settings, responder, customer_id):
    enqueue_message(conn, "m1", "Hi, this is the owner", echo_app="263902037430900")
    enqueue_message(conn, "m2", "thanks!")
    run(conn, processor, settings)
    run(conn, processor, settings)
    assert responder.sent == []
    conv = conn.execute("SELECT bot_enabled, handoff_reason FROM conversations").fetchone()
    assert tuple(conv) == (0, "staff replied manually")


def test_attachment_hands_off(conn, processor, settings, responder):
    enqueue_message(conn, "m1", None)
    run(conn, processor, settings)
    assert "passed this to our team" in responder.sent[0][1]
    assert conn.execute("SELECT bot_enabled FROM conversations").fetchone()[0] == 0


def test_send_failure_retries_without_duplicate_history(conn, settings, biz):
    responder = FakeResponder(fail_times=1)
    processor = MessageProcessor(settings, biz, responder)
    enqueue_message(conn, "m1", "hello")
    run(conn, processor, settings)
    assert queue.stats(conn) == {"pending": 1}
    run(conn, processor, settings, now=NOW.replace(minute=1))
    assert queue.stats(conn) == {"done": 1}
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE role = 'customer'").fetchone()[0] == 1
    assert len(responder.sent) == 1

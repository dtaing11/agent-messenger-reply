"""Owner alerts on Facebook Messenger: linking, buttons and commands, Meta's 24-hour window,
the daily check-in, the text backup, and the portal page."""

import asyncio
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import SecretStr

from app.agent.state_machine import OrderStatus
from app.services import owner_accounts, owner_commands
from app.timeutil import to_db
from app.workers import notifications
from app.workers.message_worker import MessageProcessor, housekeeping_once, run_once
from tests.conftest import NOW
from tests.test_admin import logged_in, portal  # noqa: F401 - fixtures
from tests.test_owner_flow import place_order
from tests.test_worker import FakeResponder, enqueue_message

CT = ZoneInfo("America/Chicago")
OWNER_PSID = "OWNER1"


@pytest.fixture
def proc(settings, biz):
    calls = []

    async def agent(conn, cid, s, b, now):
        calls.append(cid)
        return "customer reply"

    responder = FakeResponder()
    p = MessageProcessor(settings, biz, responder, agent=agent)
    p.calls, p.responder = calls, responder
    return p


def run(conn, proc, settings, now=NOW):
    asyncio.run(run_once(conn, proc, settings, now))


def link_owner(conn, label="Dina", psid=OWNER_PSID, now=NOW):
    code = owner_accounts.create_link_code(conn, label, "dina", now)
    return owner_accounts.link(conn, "PAGE1", psid, code, now)


def rows(conn, kind):
    return conn.execute("SELECT * FROM notifications WHERE type = ? ORDER BY id", (kind,)).fetchall()


# ---- linking

def test_link_with_code_from_messenger(conn, settings, proc):
    code = owner_accounts.create_link_code(conn, "Dina", "dina", NOW)
    enqueue_message(conn, "m1", f"link {code}", psid=OWNER_PSID)
    run(conn, proc, settings)
    assert "You're linked as Dina" in proc.responder.sent[-1][1] and "check-in" in proc.responder.sent[-1][1]
    assert owner_accounts.find(conn, "PAGE1", OWNER_PSID)["label"] == "Dina"
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0  # not a customer
    fyi = rows(conn, "owner_linked")
    assert {r["recipient"] for r in fyi} == set(settings.owner_phone_numbers)  # the other owners hear about it
    assert proc.calls == []
    # the code works once
    enqueue_message(conn, "m2", f"LINK {code}", psid="SOMEONE")
    run(conn, proc, settings)
    assert "isn't valid anymore" in proc.responder.sent[-1][1]
    assert owner_accounts.find(conn, "PAGE1", "SOMEONE") is None


def test_expired_code_and_customers_untouched(conn, settings, proc):
    code = owner_accounts.create_link_code(conn, "Ben", "dina", NOW - timedelta(minutes=16))
    with pytest.raises(owner_accounts.LinkError):
        owner_accounts.link(conn, "PAGE1", OWNER_PSID, code, NOW)
    assert owner_accounts.link_code_in("my zip is 123456") is None
    with pytest.raises(owner_accounts.LinkError):
        owner_accounts.create_link_code(conn, "  ", "dina", NOW)
    enqueue_message(conn, "m1", "1 dozen glazed please")
    run(conn, proc, settings)
    assert proc.calls and proc.responder.sent[-1][1] == "customer reply"


# ---- alerts and commands

def test_new_order_goes_to_messenger_with_buttons_and_no_text(conn, customer_id, biz, settings):
    link_owner(conn, now=NOW)
    o = place_order(conn, customer_id, biz, settings)
    sent = rows(conn, "new_order")
    assert [(r["channel"], r["recipient"]) for r in sent] == [("messenger", OWNER_PSID)]
    assert json.loads(sent[0]["buttons"]) == [[f"Accept #{o['id']}", f"YES {o['id']}"], [f"Decline #{o['id']}", f"NO {o['id']}"]]


def test_text_backup_when_no_owner_can_be_reached(conn, customer_id, biz, settings):
    link_owner(conn, now=NOW - timedelta(hours=25))  # window closed
    place_order(conn, customer_id, biz, settings)
    channels = sorted(r["channel"] for r in rows(conn, "new_order"))
    assert channels == ["messenger", "sms", "sms"]  # held on Messenger, and texted


def test_both_and_sms_modes(conn, biz, settings):
    link_owner(conn, now=NOW)
    both = settings.model_copy(update={"owner_alert_channel": "both"})
    sms = settings.model_copy(update={"owner_alert_channel": "sms"})
    from app.services import notify

    assert len(notify.owner_recipients(conn, both, NOW)) == 3
    assert [c for c, _ in notify.owner_recipients(conn, sms, NOW)] == ["sms", "sms"]
    assert notify.owner_recipients(conn, both, NOW, exclude="+1 (555) 000-0001") == [("messenger", OWNER_PSID), ("sms", "+15550000002")]


def test_tap_accept_on_messenger(conn, customer_id, biz, settings, proc):
    link_owner(conn, now=NOW)
    o = place_order(conn, customer_id, biz, settings)
    enqueue_message(conn, "m1", f"Accept #{o['id']}", psid=OWNER_PSID, quick_reply=f"OWNER:YES {o['id']}")
    run(conn, proc, settings)
    assert conn.execute("SELECT status FROM orders WHERE id = ?", (o["id"],)).fetchone()[0] == str(OrderStatus.ACCEPTED)
    assert proc.responder.sent[-1][1].startswith(f"Order #{o['id']} accepted.")
    actor = conn.execute("SELECT actor FROM order_events WHERE to_status = 'ACCEPTED'").fetchone()[0]
    assert actor == "owner_fb:Dina"
    assert proc.calls == []  # the AI never saw it


def test_owner_commands_work_while_paused_and_echoes_ignored(conn, settings, proc):
    link_owner(conn, now=NOW)
    paused = settings.model_copy(update={"bot_mode": "paused"})
    proc.snapshot = lambda c: paused
    enqueue_message(conn, "m1", "orders", psid=OWNER_PSID)
    run(conn, proc, paused)
    assert proc.responder.sent[-1][1] == "No open orders."
    enqueue_message(conn, "m2", "hi from the page inbox", psid=OWNER_PSID, echo_app="999")
    run(conn, proc, paused)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_like_sticker_keeps_alerts_on(conn, settings, proc):
    link_owner(conn, now=NOW - timedelta(hours=20))
    enqueue_message(conn, "m1", None, psid=OWNER_PSID)  # a like or sticker: no text
    run(conn, proc, settings)
    assert "alerts stay on" in proc.responder.sent[-1][1]
    acc = owner_accounts.find(conn, "PAGE1", OWNER_PSID)
    assert owner_accounts.window_closes_at(acc) > NOW + timedelta(hours=23)


# ---- sending and the 24-hour window

def test_send_held_then_delivered_when_owner_writes(conn, customer_id, biz, settings, proc):
    link_owner(conn, now=NOW - timedelta(hours=25))
    place_order(conn, customer_id, biz, settings)
    got = []

    async def fb(s, psid, body, buttons):
        got.append((psid, body.splitlines()[0], buttons))
        return "mid.1"

    async def sms(s, to, body, cb):
        return "SM1"

    t = datetime.now(NOW.tzinfo) + timedelta(minutes=1)
    counts = asyncio.run(notifications.send_due(conn, settings, t, sender=sms, fb_sender=fb))
    assert counts["held"] == 1 and counts["sent"] == 2 and got == []
    held = conn.execute("SELECT * FROM notifications WHERE channel = 'messenger'").fetchone()
    assert held["status"] == "failed" and held["last_error"].startswith("window_closed")
    # the owner writes: the held alert is queued again and goes out
    enqueue_message(conn, "m1", "hi", psid=OWNER_PSID)
    run(conn, proc, settings, now=t)
    assert "1 alert you missed while away is on the way" in proc.responder.sent[-1][1]
    counts = asyncio.run(notifications.send_due(conn, settings, t + timedelta(seconds=1), sender=sms, fb_sender=fb))
    assert counts["sent"] == 1 and got[0][1].startswith("NEW ORDER #") and got[0][2][0][1].startswith("YES ")


def _http_error(status, err):
    req = httpx.Request("POST", "https://graph.facebook.com/v21.0/me/messages")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(status, json={"error": err}, request=req))


def test_messenger_errors_window_vs_failure(conn, settings, monkeypatch):
    t = datetime.now(NOW.tzinfo) + timedelta(minutes=1)  # rows are due by the real clock
    link_owner(conn, now=t)

    class Client:
        error = None

        def __init__(self, s):
            pass

        async def send_text(self, psid, text, messaging_type="RESPONSE"):
            raise Client.error

    monkeypatch.setattr(notifications, "MessengerClient", Client)
    s = settings.model_copy(update={"meta_page_access_token": SecretStr("tok")})
    for i, (err, status, outcome) in enumerate([
        (_http_error(400, {"code": 10, "error_subcode": 2018278, "message": "outside window"}), "failed", "held"),
        (_http_error(400, {"code": 551, "message": "This person isn't available right now."}), "failed", "failed"),
        (_http_error(500, {"code": 2, "message": "temporary"}), "pending", "retry"),
    ]):
        Client.error = err
        conn.execute("INSERT INTO notifications (type, recipient, body, idempotency_key, channel) VALUES ('t', ?, 'x', ?, 'messenger')",
                     (OWNER_PSID, f"k{i}"))
        counts = asyncio.run(notifications.send_due(conn, s, t))
        assert counts[outcome] == 1, (outcome, counts)
        assert conn.execute("SELECT status FROM notifications WHERE idempotency_key = ?", (f"k{i}",)).fetchone()[0] == status
    assert "551" in conn.execute("SELECT last_error FROM notifications WHERE idempotency_key = 'k1'").fetchone()[0]


# ---- daily check-in

def test_checkin_before_the_window_closes_not_at_night(conn, settings, biz):
    acc = link_owner(conn, now=datetime(2026, 11, 18, 10, 0, tzinfo=CT))  # window closes Thu 10 AM
    tz = biz.timezone
    assert not owner_accounts.due_for_checkin(acc, datetime(2026, 11, 18, 15, 0, tzinfo=CT), tz)  # plenty of time left
    assert not owner_accounts.due_for_checkin(acc, datetime(2026, 11, 19, 6, 0, tzinfo=CT), tz)  # night
    assert owner_accounts.due_for_checkin(acc, datetime(2026, 11, 19, 8, 0, tzinfo=CT), tz)  # 2 hours left
    # closes Thu 10 AM, before owners have had 3 waking hours: ask the evening before instead
    assert owner_accounts.due_for_checkin(acc, datetime(2026, 11, 18, 20, 0, tzinfo=CT), tz)
    evening = link_owner(conn, label="Ben", psid="OWNER2", now=datetime(2026, 11, 18, 18, 0, tzinfo=CT))  # closes Thu 6 PM
    assert not owner_accounts.due_for_checkin(evening, datetime(2026, 11, 18, 20, 30, tzinfo=CT), tz)
    assert owner_accounts.due_for_checkin(evening, datetime(2026, 11, 19, 15, 0, tzinfo=CT), tz)


def test_checkin_sent_once_and_tap_renews(conn, settings, biz, proc):
    link_owner(conn, now=datetime(2026, 11, 18, 10, 0, tzinfo=CT))
    at = datetime(2026, 11, 19, 8, 0, tzinfo=CT)
    housekeeping_once(conn, settings, biz, at)
    housekeeping_once(conn, settings, biz, at + timedelta(minutes=5))
    sent = rows(conn, "owner_checkin")
    assert len(sent) == 1 and "tap 👍" in sent[0]["body"]
    assert json.loads(sent[0]["buttons"])[0] == ["👍 Keep alerts on", "KEEPALIVE"]
    enqueue_message(conn, "m1", "👍 Keep alerts on", psid=OWNER_PSID, quick_reply="OWNER:KEEPALIVE")
    run(conn, proc, settings, now=at + timedelta(minutes=10))
    assert "another 24 hours" in proc.responder.sent[-1][1]
    acc = owner_accounts.find(conn, "PAGE1", OWNER_PSID)
    assert owner_accounts.window_closes_at(acc) == at + timedelta(hours=24, minutes=10)
    assert not owner_accounts.due_for_checkin(acc, at + timedelta(hours=1), biz.timezone)


def test_no_checkins_in_text_only_mode(conn, settings, biz):
    link_owner(conn, now=datetime(2026, 11, 18, 10, 0, tzinfo=CT))
    sms = settings.model_copy(update={"owner_alert_channel": "sms"})
    housekeeping_once(conn, sms, biz, datetime(2026, 11, 19, 8, 0, tzinfo=CT))
    assert rows(conn, "owner_checkin") == []


def test_pickup_check_has_buttons(conn, customer_id, biz, settings):
    link_owner(conn, now=NOW)
    o = place_order(conn, customer_id, biz, settings)
    owner_commands.apply_status(conn, settings, biz, o["id"], OrderStatus.ACCEPTED, "test", NOW)
    owner_accounts.touch(conn, owner_accounts.find(conn, "PAGE1", OWNER_PSID)["id"], datetime(2026, 11, 19, 7, 0, tzinfo=CT))
    owner_commands.queue_pickup_checks(conn, settings, biz, datetime(2026, 11, 19, 8, 30, tzinfo=CT))
    r = rows(conn, "pickup_check")[0]
    assert json.loads(r["buttons"]) == [[f"Picked up #{o['id']}", f"PICKED {o['id']}"], [f"No-show #{o['id']}", f"NOSHOW {o['id']}"]]


# ---- portal

def test_owner_alerts_page(logged_in, settings):  # noqa: F811
    from app.database.connection import connect

    r = logged_in.get("/admin/owners")
    assert r.status_code == 200 and "Owners on Messenger" in r.text and "nobody is linked yet" in r.text
    r = logged_in.post("/admin/owners/link-code", data={"label": "Dina"})
    assert "LINK " in r.text and "15 minutes" in r.text
    r = logged_in.post("/admin/owners/channel", data={"owner_alert_channel": "both"}, headers={"HX-Request": "true"})
    assert r.status_code == 204
    assert "selected>Messenger and text" in logged_in.get("/admin/owners").text.replace('" selected', ' selected').replace("'both' %}", "")\
        or 'value="both" selected' in logged_in.get("/admin/owners").text
    conn = connect(settings.database_path)
    conn.execute("""INSERT INTO notifications (type, recipient, body, idempotency_key, channel, status, last_error, created_at)
                    VALUES ('new_order', '+15550000001', 'x', 'k', 'sms', 'failed', 'Twilio failed (code 30034)', ?)""",
                 (to_db(datetime.now(NOW.tzinfo)),))
    conn.close()
    r = logged_in.get("/admin/owners")
    assert "text backup isn't getting through" in r.text and "30034" in r.text
    assert logged_in.post("/admin/owners/channel", data={"owner_alert_channel": "pigeon"}).text.count("Pick one") == 1

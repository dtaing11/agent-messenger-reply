"""Owner texts (Twilio), pickup checks and no-shows, phone numbers, flavors, customer pages."""

import asyncio
from datetime import timedelta
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.agent.prompts import system_prompt
from app.agent.state_machine import OrderStatus
from app.config import get_settings
from app.integrations import twilio
from app.main import create_app
from app.services import cart, customers, notify, owner_commands
from app.services import order as order_svc
from app.services.menu import seed_menu
from app.timeutil import local, to_db, utcnow
from app.workers import notifications, queue
from app.workers.message_worker import MessageProcessor, housekeeping_once, run_once
from tests.conftest import NOW, ROOT
from tests.test_admin import logged_in, portal  # noqa: F401 - fixtures
from tests.test_orders import PICKUP
from tests.test_worker import FakeResponder

OWNER1, OWNER2 = "+15550000001", "+15550000002"


def place_order(conn, customer_id, biz, settings, name="Jessica", phone="225-555-0123", now=NOW):
    cart.add_item(conn, customer_id, "glazed_dozen", 1)
    cart.set_pickup_details(conn, customer_id, pickup_time=PICKUP, customer_name=name, customer_phone=phone)
    s = order_svc.present_order_summary(conn, customer_id, biz, settings, now)
    return order_svc.submit_order(conn, customer_id, s.token, biz, settings, now)


def new_customer(conn, psid):
    from app.services.conversation import get_or_create_customer, record_message

    cid = get_or_create_customer(conn, "PAGE1", psid)["id"]
    record_message(conn, cid, "customer", "hi")  # opens the 24h window
    conn.execute("UPDATE conversations SET last_customer_message_at = ? WHERE customer_id = ?",
                 (NOW.strftime("%Y-%m-%dT%H:%M:%S.000Z"), cid))
    return cid


# ---- phone numbers

def test_phone_required_normalized_and_shown(conn, customer_id, biz, settings):
    cart.add_item(conn, customer_id, "glazed_dozen", 1)
    cart.set_pickup_details(conn, customer_id, pickup_time=PICKUP, customer_name="Jo")
    with pytest.raises(order_svc.OrderError, match="phone number"):
        order_svc.present_order_summary(conn, customer_id, biz, settings, NOW)
    with pytest.raises(cart.CartError, match="10-digit"):
        cart.set_pickup_details(conn, customer_id, customer_phone="12345")
    cart.set_pickup_details(conn, customer_id, customer_phone="+1 (225) 555-0123")
    s = order_svc.present_order_summary(conn, customer_id, biz, settings, NOW)
    assert "Phone: (225) 555-0123" in s.text
    o = order_svc.submit_order(conn, customer_id, s.token, biz, settings, NOW)
    assert o["customer_phone"] == "+12255550123"
    row = conn.execute("SELECT name, phone FROM customers WHERE id = ?", (customer_id,)).fetchone()
    assert tuple(row) == ("Jo", "+12255550123")
    sms = conn.execute("SELECT body FROM notifications WHERE type = 'new_order' LIMIT 1").fetchone()["body"]
    assert "Phone: (225) 555-0123" in sms and "First order from this customer." in sms


def test_phone_optional_when_turned_off(conn, customer_id, biz, settings):
    s = settings.model_copy(update={"require_customer_phone": False})
    cart.add_item(conn, customer_id, "glazed_dozen", 1)
    cart.set_pickup_details(conn, customer_id, pickup_time=PICKUP, customer_name="Jo")
    assert order_svc.present_order_summary(conn, customer_id, biz, s, NOW).token


def test_saved_contact_offered_but_no_show_hidden_from_bot(conn, customer_id, biz, settings):
    import json

    from app.agent import tools

    o = place_order(conn, customer_id, biz, settings)
    order_svc.transition_order(conn, o["id"], OrderStatus.ACCEPTED, "owner")
    order_svc.transition_order(conn, o["id"], OrderStatus.NO_SHOW, "owner")
    ctx = tools.ToolContext(conn, customer_id, settings, biz, NOW)
    out = json.dumps(asyncio.run(tools.execute(ctx, "get_cart", {})))
    assert "(225) 555-0123" in out and "Jessica" in out
    for word in ("no_show", "NO_SHOW", "missed", "no-show"):
        assert word not in out
    prompt = system_prompt(biz, local(NOW, biz.timezone))
    assert "no-show" not in prompt.lower() and "missed" not in prompt.lower()


# ---- owner commands

def test_yes_accepts_and_notifies_customer(conn, biz, settings):
    cid = new_customer(conn, "PSID9")
    o = place_order(conn, cid, biz, settings)
    reply = owner_commands.handle(conn, settings, biz, OWNER1, "yes " + str(o["id"]), NOW)
    assert f"Order #{o['id']} accepted" in reply and "Messenger message" in reply
    ev = conn.execute("SELECT * FROM webhook_events WHERE event_type = 'customer_update'").fetchone()
    assert "is confirmed" in ev["payload"] and "Oak Point Plaza" in ev["payload"]
    fyi = conn.execute("SELECT recipient, body FROM notifications WHERE type = 'owner_fyi'").fetchall()
    assert [r["recipient"] for r in fyi] == [OWNER2] and "1" in fyi[0]["body"]


def test_order_number_always_required_and_commands_exact(conn, customer_id, biz, settings):
    o = place_order(conn, customer_id, biz, settings)
    reply = owner_commands.handle(conn, settings, biz, OWNER1, "YES", NOW)
    assert f"Which order? Reply YES and the order number, e.g. YES {o['id']}" in reply and "Jessica" in reply
    assert conn.execute("SELECT status FROM orders").fetchone()[0] == "PENDING_ACCEPTANCE"
    for chat in ("ok", f"Ok see you at {o['id']}", "No problem, thanks!", f"yes {o['id']} and {o['id'] + 1}",
                 f"ready in {o['id']} min", f"no {o['id']} worries"):
        assert "nothing was changed" in owner_commands.handle(conn, settings, biz, OWNER1, chat, NOW), chat
    assert conn.execute("SELECT status FROM orders").fetchone()[0] == "PENDING_ACCEPTANCE"
    assert "accepted" in owner_commands.handle(conn, settings, biz, OWNER1, f" yes #{o['id']}. ", NOW)
    assert "can't be accepted" in owner_commands.handle(conn, settings, biz, OWNER1, f"YES {o['id']}", NOW)
    assert "No order can be declined" in owner_commands.handle(conn, settings, biz, OWNER1, "NO", NOW)


def test_noshow_and_warning_on_next_order(conn, customer_id, biz, settings):
    o = place_order(conn, customer_id, biz, settings)
    owner_commands.handle(conn, settings, biz, OWNER1, f"YES {o['id']}", NOW)
    reply = owner_commands.handle(conn, settings, biz, OWNER1, f"no show {o['id']}", NOW)
    assert "no-show" in reply and "1 missed pickup" in reply and "isn't told" in reply
    assert conn.execute("SELECT status FROM orders WHERE id = ?", (o["id"],)).fetchone()[0] == "NO_SHOW"
    # no Messenger message for a no-show
    assert not conn.execute("SELECT 1 FROM webhook_events WHERE external_event_id LIKE ?", (f"order:{o['id']}:NO_SHOW",)).fetchone()
    o2 = place_order(conn, customer_id, biz, settings, now=NOW + timedelta(minutes=1))
    sms = conn.execute("SELECT body FROM notifications WHERE order_id = ? AND type = 'new_order'", (o2["id"],)).fetchone()["body"]
    assert "HEADS UP: this customer didn't pick up 1 past order" in sms and f"#{o['id']}" in sms


def test_noshow_stays_with_the_messenger_account_not_the_phone(conn, customer_id, biz, settings):
    o = place_order(conn, customer_id, biz, settings, phone="225-555-0199")
    order_svc.transition_order(conn, o["id"], OrderStatus.ACCEPTED, "owner")
    order_svc.transition_order(conn, o["id"], OrderStatus.NO_SHOW, "owner")
    other = new_customer(conn, "PSID-OTHER")  # someone else who gives the same number
    assert customers.pickup_history(conn, other).no_shows == 0
    assert customers.pickup_history(conn, customer_id).no_shows == 1


def test_picked_up_and_fixing_a_mistaken_noshow(conn, customer_id, biz, settings):
    o = place_order(conn, customer_id, biz, settings)
    owner_commands.handle(conn, settings, biz, OWNER1, f"Y {o['id']}", NOW)
    owner_commands.handle(conn, settings, biz, OWNER1, f"NOSHOW {o['id']}", NOW)
    assert "picked up" in owner_commands.handle(conn, settings, biz, OWNER1, f"picked up {o['id']}", NOW)
    h = customers.pickup_history(conn, customer_id)
    assert (h.picked_up, h.no_shows) == (1, 0)


def test_orders_pause_resume_help(conn, customer_id, biz, settings):
    place_order(conn, customer_id, biz, settings)
    assert "Jessica" in owner_commands.handle(conn, settings, biz, OWNER1, "orders", NOW)
    assert "paused" in owner_commands.handle(conn, settings, biz, OWNER1, "PAUSE", NOW)
    assert conn.execute("SELECT value FROM app_settings WHERE key = 'bot_mode'").fetchone()[0] == '"paused"'
    assert "live" in owner_commands.handle(conn, settings, biz, OWNER1, "resume", NOW)
    assert "PICKED" in owner_commands.handle(conn, settings, biz, OWNER1, "huh?", NOW)


def test_pickup_check_asks_once_after_grace(conn, customer_id, biz, settings):
    o = place_order(conn, customer_id, biz, settings)
    owner_commands.handle(conn, settings, biz, OWNER1, f"YES {o['id']}", NOW)
    assert owner_commands.queue_pickup_checks(conn, settings, biz, PICKUP + timedelta(minutes=10)) == []
    later = PICKUP + timedelta(minutes=31)
    assert owner_commands.queue_pickup_checks(conn, settings, biz, later) == [o["id"]]
    assert owner_commands.queue_pickup_checks(conn, settings, biz, later) == []
    rows = conn.execute("SELECT recipient, body FROM notifications WHERE type = 'pickup_check'").fetchall()
    assert len(rows) == 2 and f"PICKED {o['id']} or NOSHOW {o['id']}" in rows[0]["body"]
    assert "(225) 555-0123" in rows[0]["body"]


def test_expiry_tells_customer_and_owners(conn, biz, settings):
    cid = new_customer(conn, "PSID7")
    o = place_order(conn, cid, biz, settings)
    housekeeping_once(conn, settings, biz, NOW + timedelta(hours=3))
    assert conn.execute("SELECT status FROM orders WHERE id = ?", (o["id"],)).fetchone()[0] == "EXPIRED"
    assert conn.execute("SELECT COUNT(*) FROM notifications WHERE type = 'order_expired'").fetchone()[0] == 2
    assert queue.stats(conn).get("pending") == 1


def test_customer_update_sent_inside_window_only(conn, biz, settings):
    cid = new_customer(conn, "PSID8")
    o = place_order(conn, cid, biz, settings)
    acc = order_svc.transition_order(conn, o["id"], OrderStatus.ACCEPTED, "owner")
    notify.queue_customer_update(conn, acc, OrderStatus.ACCEPTED, biz, NOW)
    responder = FakeResponder()
    proc = MessageProcessor(settings, biz, responder)
    asyncio.run(run_once(conn, proc, settings, NOW))
    assert responder.sent[0][2] == "UPDATE" and "confirmed" in responder.sent[0][1]
    # outside the 24h window: not sent
    ready = order_svc.transition_order(conn, o["id"], OrderStatus.READY, "owner")
    notify.queue_customer_update(conn, ready, OrderStatus.READY, biz, NOW)
    asyncio.run(run_once(conn, proc, settings, NOW + timedelta(hours=25)))
    assert len(responder.sent) == 1


# ---- sending owner texts

def test_notification_sender_retries_and_gives_up(conn, customer_id, biz, settings):
    place_order(conn, customer_id, biz, settings)
    t = utcnow() + timedelta(minutes=1)  # rows are due from the real clock
    sent = []

    async def ok(s, to, body, cb):
        sent.append(to)
        return "SM1"

    counts = asyncio.run(notifications.send_due(conn, settings, t, sender=ok))
    assert counts["sent"] == 2 and sorted(sent) == [OWNER1, OWNER2]
    assert {r[0] for r in conn.execute("SELECT status FROM notifications")} == {"sent"}

    notify.alert_owners(conn, settings, "handoff", "help", "k1")

    async def flaky(s, to, body, cb):
        raise twilio.SMSError("HTTP 503", retryable=True)

    assert asyncio.run(notifications.send_due(conn, settings, t, sender=flaky))["retry"] == 2
    assert asyncio.run(notifications.send_due(conn, settings, t, sender=flaky))["retry"] == 0  # backing off

    async def bad(s, to, body, cb):
        raise twilio.SMSError("HTTP 400 code 21211: invalid To", retryable=False)

    later = t + timedelta(hours=1)
    assert asyncio.run(notifications.send_due(conn, settings, later, sender=bad))["failed"] == 2


def test_sender_waits_until_twilio_configured(conn, customer_id, biz, settings):
    place_order(conn, customer_id, biz, settings)
    assert asyncio.run(notifications.send_due(conn, settings, NOW)) == {"sent": 0, "retry": 0, "failed": 0, "held": 0}
    assert conn.execute("SELECT MAX(attempts) FROM notifications").fetchone()[0] == 0


def test_send_sms_request_shape(settings):
    s = settings.model_copy(update={"twilio_account_sid": "AC1", "twilio_auth_token": SecretStr("tok"),
                                    "twilio_phone_number": "+15559990000"})
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(201, json={"sid": "SM123"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await twilio.send_sms(s, OWNER1, "hello", "https://x.example/webhooks/sms/status", http=http)

    assert asyncio.run(go()) == "SM123"
    assert seen["url"].endswith("/Accounts/AC1/Messages.json") and "From=%2B15559990000" in seen["body"]
    assert seen["auth"].startswith("Basic ")


def test_twilio_signature_matches_documented_example():
    # Example from Twilio's webhook security docs.
    params = [("CallSid", "CA1234567890ABCDE"), ("Caller", "+12349013030"), ("Digits", "1234"),
              ("From", "+12349013030"), ("To", "+18005551212")]
    sig = twilio.expected_signature("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", params)
    assert sig in ("0/KCTR6DLpKmkAf8muzZqo1nDgQ=", "RSOYDt4T1cUTdK1PDd93/VVr8B8=")
    assert twilio.valid_signature("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", params, sig)
    assert not twilio.valid_signature("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", params, "x" + sig)


# ---- the SMS webhook

@pytest.fixture
def sms_client(settings, monkeypatch):
    s = settings.model_copy(update={"twilio_auth_token": SecretStr("tw-token"), "public_base_url": "https://bot.example.com",
                                    "twilio_account_sid": "AC1", "twilio_phone_number": "+15559990000"})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: s
    monkeypatch.setattr("app.main.get_settings", lambda: s)
    with TestClient(app) as c:
        yield c, s


def post_sms(client, body: dict, token="tw-token", path="/webhooks/sms"):
    sig = twilio.expected_signature(token, f"https://bot.example.com{path}", list(body.items()))
    return client.post(path, content=urlencode(body),
                       headers={"X-Twilio-Signature": sig, "Content-Type": "application/x-www-form-urlencoded"})


def _pickup_tomorrow(conn, order_id):
    conn.execute("UPDATE orders SET pickup_time = ?, expires_at = NULL WHERE id = ?",
                 (to_db(utcnow() + timedelta(days=1)), order_id))


def test_sms_webhook_owner_command(sms_client, conn, customer_id, biz):
    client, s = sms_client
    o = place_order(conn, customer_id, biz, s)
    _pickup_tomorrow(conn, o["id"])  # the webhook runs on the real clock
    r = post_sms(client, {"From": OWNER1, "To": "+15559990000", "Body": f"YES {o['id']}", "MessageSid": "SMx"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    assert f"Order #{o['id']} accepted" in r.text
    assert conn.execute("SELECT status FROM orders WHERE id = ?", (o["id"],)).fetchone()[0] == "ACCEPTED"


def test_sms_webhook_rejects_bad_signature_and_strangers(sms_client, conn, customer_id, biz):
    client, s = sms_client
    o = place_order(conn, customer_id, biz, s)
    r = post_sms(client, {"From": OWNER1, "Body": f"YES {o['id']}"}, token="wrong")
    assert r.status_code == 403
    r = post_sms(client, {"From": "+12255559999", "Body": f"YES {o['id']}"})
    assert r.status_code == 200 and "<Message>" not in r.text
    assert conn.execute("SELECT status FROM orders WHERE id = ?", (o["id"],)).fetchone()[0] == "PENDING_ACCEPTANCE"


def test_sms_status_callback_marks_delivery(sms_client, conn, customer_id, biz):
    client, s = sms_client
    place_order(conn, customer_id, biz, s)
    conn.execute("UPDATE notifications SET status = 'sent', provider_id = 'SM9' WHERE recipient = ?", (OWNER1,))
    r = post_sms(client, {"MessageSid": "SM9", "MessageStatus": "delivered"}, path="/webhooks/sms/status")
    assert r.status_code == 204
    assert conn.execute("SELECT status FROM notifications WHERE provider_id = 'SM9'").fetchone()[0] == "delivered"


# ---- flavors (required choices) on the real menu

def test_twist_and_long_john_need_a_flavor(conn, customer_id):
    seed_menu(conn, ROOT / "data/menu.json")
    with pytest.raises(cart.DetailsNeeded) as exc:
        cart.add_item(conn, customer_id, "twist", 2)
    assert "Cinnamon Glazed, Cinnamon Sugar, Chocolate or Glazed" in exc.value.questions[0]
    with pytest.raises(cart.DetailsNeeded):
        cart.add_item(conn, customer_id, "twist", 1, details={"flavor": "cinnamon"})  # ambiguous: ask
    view = cart.add_item(conn, customer_id, "twist", 1, details={"flavor": "chocolate"})
    view = cart.add_item(conn, customer_id, "twist", 1, details={"flavor": "Glazed"})
    assert [l.details for l in view.lines] == [{"flavor": "Chocolate"}, {"flavor": "Glazed"}]
    view = cart.add_item(conn, customer_id, "long_john", 1, details={"flavor": "maple"})
    assert view.lines[-1].details == {"flavor": "Maple"}
    with pytest.raises(cart.CartError):
        cart.add_item(conn, customer_id, "apple_fritter", 1, details={"flavor": "Maple"})


def test_flavor_shown_in_summary_and_owner_text(conn, customer_id, biz, settings):
    seed_menu(conn, ROOT / "data/menu.json")
    cart.add_item(conn, customer_id, "twist", 3, details={"flavor": "Cinnamon Sugar"})
    cart.set_pickup_details(conn, customer_id, pickup_time=PICKUP, customer_name="Al", customer_phone="2255550123")
    s = order_svc.present_order_summary(conn, customer_id, biz, settings, NOW)
    assert "3 x Twist" in s.text and "Flavor: Cinnamon Sugar" in s.text
    o = order_svc.submit_order(conn, customer_id, s.token, biz, settings, NOW)
    sms = order_svc.format_owner_sms(conn, o, biz)
    assert "Flavor: Cinnamon Sugar" in sms


def test_biscuits_and_burritos_are_separate(conn):
    from app.services import menu

    seed_menu(conn, ROOT / "data/menu.json")
    for base in ("sausage", "bacon", "ham"):
        b = menu.get_item(conn, f"{base}_egg_cheese_biscuit")
        u = menu.get_item(conn, f"{base}_egg_cheese_burrito")
        assert b["price_cents"] == u["price_cents"] == 365 and b["available"] and u["available"]
        assert "or" not in b["name"].split()
    assert menu.get_item(conn, "plain_biscuit")["price_cents"] == 179
    names = [r["name"] for r in menu.search_menu(conn, "bacon egg cheese")]
    assert "Bacon, Egg & Cheese Biscuit" in names and "Bacon, Egg & Cheese Burrito" in names


def test_menu_editor_sets_choices(conn):
    from app.services import menu, menu_admin

    menu_admin.update_item(conn, "glazed_single", {"price": "1.10", "available": "on", "choice_label": "Flavor",
                                                   "choice_options": "Plain, Chocolate"})
    assert "Chocolate" in menu.get_item(conn, "glazed_single")["choices"]
    menu_admin.update_item(conn, "glazed_single", {"price": "1.10", "available": "on", "choice_options": ""})
    assert menu.get_item(conn, "glazed_single")["choices"] is None


def test_prompt_name_and_credit(biz):
    p = system_prompt(biz, local(NOW, biz.timezone))
    assert p.startswith("You are Sprinkle")
    assert "built by Dina Taing" in p and "https://www.dinataing.dev/" in p and "@" not in p


# ---- portal pages

def test_orders_and_customer_pages(logged_in, settings, biz):  # noqa: F811
    from app.database.connection import connect
    from app.services.conversation import get_or_create_customer

    conn = connect(settings.database_path)
    cid = get_or_create_customer(conn, "PAGE1", "PSID55")["id"]
    o = place_order(conn, cid, biz, settings)
    _pickup_tomorrow(conn, o["id"])  # the portal runs on the real clock
    r = logged_in.get("/admin/orders")
    assert "(225) 555-0123" in r.text and "Accept" in r.text
    r = logged_in.post(f"/admin/orders/{o['id']}/status", data={"target": "ACCEPTED"}, headers={"HX-Request": "true"})
    assert r.status_code == 204
    r = logged_in.post(f"/admin/orders/{o['id']}/status", data={"target": "NO_SHOW"}, headers={"HX-Request": "true"})
    assert r.status_code == 204
    r = logged_in.get(f"/admin/customers/{cid}")
    assert "No-show" in r.text and "Jessica" in r.text
    assert "Jessica" in logged_in.get("/admin/customers").text
    conn.execute("UPDATE conversations SET bot_enabled = 0, handoff_reason = 'refund' WHERE customer_id = ?", (cid,))
    assert "Resume the bot" in logged_in.get(f"/admin/customers/{cid}").text
    r = logged_in.post(f"/admin/customers/{cid}/bot", data={"enabled": "1"}, headers={"HX-Request": "true"})
    assert r.status_code == 204
    assert conn.execute("SELECT bot_enabled FROM conversations WHERE customer_id = ?", (cid,)).fetchone()[0] == 1
    conn.close()


def test_big_donuts_are_every_209_donut(conn, customer_id, settings, biz):
    import json as _json

    from app.agent import tools
    from app.services import menu, menu_admin

    seed_menu(conn, ROOT / "data/menu.json")
    names = [r["name"] for r in menu.pool_items(conn, "donuts", 209)]
    assert set(names) == {"Cinnamon Roll", "Pinecone", "Twist", "Long John"}
    ctx = tools.ToolContext(conn, customer_id, settings, biz, NOW)
    res = asyncio.run(tools.execute(ctx, "search_menu", {"query": "half dozen big donuts"}))
    item = next(i for i in res["items"] if i["item_id"] == "half_dozen_big_donuts")
    assert any("Twist (Cinnamon Glazed" in c for c in item["pick_a_mix"]["choose_from"]) and item["pick_a_mix"]["how_many"] == 6
    with pytest.raises(cart.DetailsNeeded) as exc:
        cart.add_item(conn, customer_id, "half_dozen_big_donuts", 1)
    assert "Pinecone" in exc.value.questions[0] and "Which 6 Big Donuts" in exc.value.questions[0]
    view = cart.add_item(conn, customer_id, "half_dozen_big_donuts", 1,
                         details={"big_donuts": "2 Twist (Chocolate), 2 Cinnamon Roll, 2 Pinecone"})
    assert view.lines[0].details == {"big_donuts": "2 Twist (Chocolate), 2 Cinnamon Roll, 2 Pinecone"}
    # a new $2.09 donut joins the Big Donuts automatically
    menu_admin.add_item(conn, "donuts", "Bear Claw", "2.09")
    res = asyncio.run(tools.execute(ctx, "search_menu", {"query": "big donuts"}))
    item = next(i for i in res["items"] if i["item_id"] == "half_dozen_big_donuts")
    assert "Bear Claw" in item["pick_a_mix"]["choose_from"]
    assert order_svc._detail_lines(view.lines[0].details, "  ") == ["  Big donuts: 2 Twist (Chocolate), 2 Cinnamon Roll, 2 Pinecone"]
    _json.dumps(item)


def test_icing_donut_flavor_and_sprinkles_rules(conn, customer_id):
    seed_menu(conn, ROOT / "data/menu.json")
    with pytest.raises(cart.DetailsNeeded) as exc:
        cart.add_item(conn, customer_id, "icing_sprinkles_donut", 1)
    assert len(exc.value.questions) == 2 and "Strawberry Icing" in exc.value.questions[0]
    with pytest.raises(cart.DetailsNeeded) as exc:
        cart.add_item(conn, customer_id, "icing_sprinkles_donut", 1, details={"flavor": "chocolate icing"})
    assert exc.value.questions == ["Would you like sprinkles on the Icing/Sprinkles Donut?"]
    v = cart.add_item(conn, customer_id, "icing_sprinkles_donut", 2, details={"flavor": "Chocolate Icing", "sprinkles": "yes"})
    assert v.lines[-1].details == {"flavor": "Chocolate Icing", "sprinkles": "Yes"}
    # Glazed and Cinnamon Sugar: no sprinkles question, and sprinkles are refused
    for flavor in ("Glazed", "Cinnamon Sugar"):
        v = cart.add_item(conn, customer_id, "icing_sprinkles_donut", 1, details={"flavor": flavor})
        assert v.lines[-1].details == {"flavor": flavor}
        with pytest.raises(cart.CartError, match="No sprinkles"):
            cart.add_item(conn, customer_id, "icing_sprinkles_donut", 1, details={"flavor": flavor, "sprinkles": "Yes"})
    v = cart.add_item(conn, customer_id, "icing_sprinkles_donut", 1, details={"flavor": "Glazed", "sprinkles": "No"})
    glazed = [l for l in v.lines if l.details == {"flavor": "Glazed"}]
    assert len(glazed) == 1 and glazed[0].quantity == 2  # same as the earlier Glazed line


def test_mix_donuts_need_a_mix(conn, customer_id):
    seed_menu(conn, ROOT / "data/menu.json")
    with pytest.raises(cart.DetailsNeeded) as exc:
        cart.add_item(conn, customer_id, "dozen_mix_donuts", 1)
    assert "12 donuts" in exc.value.questions[0] and "mix them for you" in exc.value.questions[0]
    v = cart.add_item(conn, customer_id, "dozen_mix_donuts", 1, details={"mix": "6 Maple Icing with sprinkles, 6 Glazed"})
    assert v.lines[-1].details == {"mix": "6 Maple Icing with sprinkles, 6 Glazed"}
    v = cart.add_item(conn, customer_id, "half_dozen_mix_donuts", 1, details={"mix": "store's choice"})
    assert v.lines[-1].details == {"mix": "store's choice"}
    with pytest.raises(cart.DetailsNeeded):
        cart.add_item(conn, customer_id, "half_dozen_mix_donuts", 1, details={"mix": "whatever is good"})


def test_editor_save_keeps_multi_choices(conn):
    from app.services import menu, menu_admin

    seed_menu(conn, ROOT / "data/menu.json")
    before = menu.get_item(conn, "icing_sprinkles_donut")["choices"]
    menu_admin.update_item(conn, "icing_sprinkles_donut", {"name": "Icing/Sprinkles Donut", "price": "1.45", "available": "on"})
    after = menu.get_item(conn, "icing_sprinkles_donut")
    assert after["choices"] == before and after["price_cents"] == 145


def test_bubble_tea_flavors_and_toppings(conn, customer_id):
    from app.services import menu

    seed_menu(conn, ROOT / "data/menu.json")
    with pytest.raises(cart.DetailsNeeded) as exc:
        cart.add_item(conn, customer_id, "milk_tea", 1)
    assert "Taro Milk Tea" in exc.value.questions[0] and "Oolong" not in exc.value.questions[0]
    mods = {m["name"]: m["id"] for m in menu.get_modifiers(conn, "milk_tea")}
    v = cart.add_item(conn, customer_id, "milk_tea", 2, [mods["Tapioca Pearl"], mods["Coffee Jelly"]],
                      {"flavor": "taro", "sweetness": "Regular"})
    line = v.lines[0]
    assert line.details == {"flavor": "Taro Milk Tea", "sweetness": "Regular"} and line.unit_price_cents == 499 + 80 + 125
    assert line.line_total_cents == 2 * (499 + 80 + 125)
    for item, price in (("green_tea", 499), ("smoothie", 599), ("iced_coffee", 399)):
        assert menu.get_item(conn, item)["price_cents"] == price
        assert len(menu.get_modifiers(conn, item)) == 7


def test_toppings_only_on_the_bubble_tea_drinks(conn):
    """Owner, 2026-11-20 (replacing "toppings on any drink"): only Green Tea, Milk Tea, Iced Coffee
    and Smoothie take toppings."""
    from app.services import menu

    seed_menu(conn, ROOT / "data/menu.json")
    drinks = conn.execute("SELECT id FROM menu_items WHERE category IN ('drinks', 'bubble_tea') AND available = 1").fetchall()
    assert len(drinks) > 4
    for (d,) in drinks:
        names = {m["name"] for m in menu.get_modifiers(conn, d)}
        if d in ("green_tea", "milk_tea", "iced_coffee", "smoothie"):
            assert {"Tapioca Pearl", "Coffee Jelly"} <= names, d
        else:
            assert not names, d

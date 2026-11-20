"""Customer photos, the owner's simple version (2026-11-20): no image AI. A photo sent while
ordering a Special Design Donut is saved with the order and shown to the owners; any other
photo goes to a person. Stickers and likes are a 👍, not a photo."""

import asyncio
import json
from datetime import timedelta

import httpx
import pytest

from app.integrations.messenger import MessengerClient
from app.services import cart, owner_accounts, photos
from app.services import conversation as conv
from app.services import order as order_svc
from app.workers import notifications, queue
from app.workers.message_worker import MessageProcessor, run_once
from tests.conftest import NOW
from tests.test_admin import logged_in, portal  # noqa: F401 - fixtures
from tests.test_owner_flow import PICKUP
from tests.test_owner_messenger import link_owner
from tests.test_worker import FakeResponder

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
URL = "https://scontent.xx.fbcdn.net/v/t1/photo.jpg?sig=abc"


def enqueue_photo(conn, mid, urls=(URL,), psid="PSID1", sticker=None):
    atts = [{"type": "image", "payload": {"url": u, **({"sticker_id": sticker} if sticker else {})}} for u in urls]
    msg = {"mid": mid, "attachments": atts}
    if sticker:
        msg["sticker_id"] = sticker
    event = {"sender": {"id": psid}, "recipient": {"id": "PAGE1"}, "message": msg}
    queue.enqueue(conn, "messenger", f"PAGE1:{mid}", "message", {"page_id": "PAGE1", "psid": psid, "event": event},
                  customer_key=f"PAGE1:{psid}", now=NOW)


class Agent:
    """Records what the assistant was given and answers with a fixed reply."""

    def __init__(self, reply="Thanks! I've saved your photo with your Special Design Donut. What should it say?"):
        self.reply, self.last_user = reply, None

    async def __call__(self, conn, cid, s, b, now):
        rows = conn.execute("SELECT content FROM messages WHERE role = 'customer' ORDER BY id DESC LIMIT 1").fetchone()
        self.last_user = rows[0]
        return self.reply


def processor(settings, biz, responder, agent=None, data=JPEG, error=None):
    async def fetch(url, max_bytes):
        if error:
            raise photos.PhotoError(error)
        return data
    return MessageProcessor(settings, biz, responder, agent=agent or Agent(), fetch_photo=fetch)


def run(conn, p, settings, now=NOW):
    return asyncio.run(run_once(conn, p, settings, now))


def _design_cart(conn, cid):
    cart.add_item(conn, cid, "special_design_donut", 1, details={"message": "Happy Bday Emma", "design": "Like the customer's photo"})


# ---- stickers and likes

def test_like_sticker_is_a_thumbs_up_not_a_handoff(conn, customer_id, settings, biz):
    agent, r = Agent("👍 Anything else?"), FakeResponder()
    enqueue_photo(conn, "s1", sticker=369239263222822)
    run(conn, processor(settings, biz, r, agent), settings)
    assert agent.last_user == "👍" and r.sent[-1][1] == "👍 Anything else?"
    assert conv.get_conversation(conn, customer_id)["bot_enabled"] == 1
    assert conn.execute("SELECT COUNT(*) FROM customer_photos").fetchone()[0] == 0


# ---- a photo for a Special Design Donut

def test_design_photo_is_saved_with_the_order_and_sent_to_owners(conn, customer_id, settings, biz):
    link_owner(conn)  # an approved owner on Messenger, window open
    _design_cart(conn, customer_id)
    agent, r = Agent(), FakeResponder()
    enqueue_photo(conn, "p1")
    run(conn, processor(settings, biz, r, agent), settings)

    row = conn.execute("SELECT * FROM customer_photos").fetchone()
    assert row["mime"] == "image/jpeg" and row["error"] is None and row["cart_id"] is not None
    assert photos.file_for(settings, row["path"]).read_bytes() == JPEG
    assert "sent a photo (#1)" in agent.last_user and "can't see photos" in agent.last_user  # never guesses
    assert r.sent[-1][1].startswith("Thanks! I've saved your photo")
    assert conv.get_conversation(conn, customer_id)["bot_enabled"] == 1

    cart.set_pickup_details(conn, customer_id, pickup_time=PICKUP, customer_name="Jo", customer_phone="225-555-0123")
    s = order_svc.present_order_summary(conn, customer_id, biz, settings, NOW)
    o = order_svc.submit_order(conn, customer_id, s.token, biz, settings, NOW)
    assert conn.execute("SELECT order_id FROM customer_photos").fetchone()[0] == o["id"]
    alerts = conn.execute("SELECT * FROM notifications WHERE type = 'new_order'").fetchall()
    fb = [a for a in alerts if a["channel"] == "messenger"]
    assert fb and json.loads(fb[0]["attachments"]) == [row["path"]]
    assert "Photo: the customer sent 1 reference photo" in fb[0]["body"]


def test_talk_about_a_design_counts_before_the_donut_is_in_the_cart(conn, customer_id, settings, biz):
    conv.record_message(conn, customer_id, "customer", "I'd like a special design donut for Saturday")
    conv.record_message(conn, customer_id, "assistant", "Sure! What design would you like?")
    agent, r = Agent(), FakeResponder()
    enqueue_photo(conn, "p2")
    run(conn, processor(settings, biz, r, agent), settings)
    assert agent.last_user.startswith("[The customer sent a photo")
    assert conn.execute("SELECT cart_id FROM customer_photos").fetchone()[0] is not None


# ---- any other photo goes to a person

def test_photo_without_a_design_donut_goes_to_staff(conn, customer_id, settings, biz):
    link_owner(conn)
    conv.record_message(conn, customer_id, "customer", "hi, what's in the breakfast croissant?")
    agent, r = Agent(), FakeResponder()
    enqueue_photo(conn, "p3")
    run(conn, processor(settings, biz, r, agent), settings)
    assert agent.last_user is None  # the AI didn't run
    assert r.sent[-1][1] == "Thanks for the photo! I've passed it to our team, and someone will reply here soon."
    c = conv.get_conversation(conn, customer_id)
    assert c["bot_enabled"] == 0 and c["handoff_reason"] == "customer sent a photo"
    fb = conn.execute("SELECT * FROM notifications WHERE type = 'handoff' AND channel = 'messenger'").fetchone()
    assert json.loads(fb["attachments"]) == [conn.execute("SELECT path FROM customer_photos").fetchone()[0]]


@pytest.mark.parametrize("kw,err", [({"error": "the photo is too large"}, "the photo is too large"),
                                    ({"data": b"<html>not an image</html>"}, "not a JPEG, PNG, GIF or WebP image")])
def test_a_photo_that_cant_be_kept_goes_to_staff(conn, customer_id, settings, biz, kw, err):
    _design_cart(conn, customer_id)
    r = FakeResponder()
    enqueue_photo(conn, "p4")
    run(conn, processor(settings, biz, r, **kw), settings)
    row = conn.execute("SELECT * FROM customer_photos").fetchone()
    assert row["path"] is None and row["error"] == err
    assert r.sent[-1][1].startswith("Sorry, I couldn't open that photo.")
    assert conv.get_conversation(conn, customer_id)["bot_enabled"] == 0


def test_photo_while_staff_have_the_chat_is_kept_quietly(conn, customer_id, settings, biz):
    conv.set_bot_enabled(conn, customer_id, False, reason="staff replied manually")
    r = FakeResponder()
    enqueue_photo(conn, "p5", urls=(URL, URL.replace("photo", "photo2")))
    run(conn, processor(settings, biz, r, data=PNG), settings)
    assert r.sent == []
    assert conn.execute("SELECT COUNT(*) FROM customer_photos WHERE mime = 'image/png'").fetchone()[0] == 2


def test_saving_the_same_photo_twice_is_one_row(conn, customer_id, settings):
    a = photos.save(conn, settings, customer_id, "m1", 0, JPEG)
    b = photos.save(conn, settings, customer_id, "m1", 0, JPEG)
    assert a.id == b.id and conn.execute("SELECT COUNT(*) FROM customer_photos").fetchone()[0] == 1


# ---- downloading: Meta's hosts only, size-capped

def _http(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_fetch_only_from_meta_hosts_and_checks_redirects():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "lookaside.fbsbx.com":
            return httpx.Response(302, headers={"location": "https://evil.example.com/x.jpg"})
        return httpx.Response(200, content=JPEG)

    assert asyncio.run(photos.fetch(URL, 10_000, _http(handler))) == JPEG
    for bad in ("http://scontent.xx.fbcdn.net/a.jpg", "https://example.com/a.jpg", "https://fbcdn.net.evil.com/a.jpg",
                "https://lookaside.fbsbx.com/r"):
        with pytest.raises(photos.PhotoError):
            asyncio.run(photos.fetch(bad, 10_000, _http(handler)))
    assert not any("evil" in u for u in seen)  # never fetched


def test_fetch_stops_at_the_size_limit():
    big = JPEG + b"\x00" * 5000
    with pytest.raises(photos.PhotoError, match="too large"):
        asyncio.run(photos.fetch(URL, 1000, _http(lambda r: httpx.Response(200, content=big))))


def test_file_for_never_leaves_the_uploads_folder(settings):
    (photos.uploads_dir(settings)).mkdir(parents=True, exist_ok=True)
    (settings.database_path.parent / "secret.txt").write_text("x")
    assert photos.file_for(settings, "../secret.txt") is None


# ---- owners get the photo on Messenger, before the text with the buttons

def test_owner_alert_sends_the_photo_then_the_text(conn, customer_id, settings):
    link_owner(conn)
    p = photos.save(conn, settings, customer_id, "m1", 0, JPEG)
    from app.services import notify
    notify.alert_owners(conn, settings, "handoff", "needs a person", "k1", attachments=[p.path], now=NOW)
    conn.execute("UPDATE notifications SET next_attempt_at = ?", (NOW.isoformat(),))
    calls = []

    async def fb(s, psid, body, buttons, attachments=None):
        calls.append((body, attachments))
        return "mid"

    async def sms(*a):
        return "SM"

    asyncio.run(notifications.send_due(conn, settings, NOW + timedelta(minutes=1), sender=sms, fb_sender=fb, limit=10))
    assert calls == [("needs a person", [p.path])]


def test_messenger_client_uploads_the_image():
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={"message_id": "m1"})

    s = __import__("app.config", fromlist=["Settings"]).Settings(_env_file=None, meta_page_access_token="tok")
    client = MessengerClient(s, http=_http(handler))
    asyncio.run(client.send_image_file("OWNER", JPEG, "image/jpeg", "a.jpg"))
    body = sent[0].content
    assert b'name="filedata"' in body and JPEG in body and b'"type": "image"' in body
    assert sent[0].headers["authorization"] == "Bearer tok"


# ---- portal

def test_portal_shows_order_photos_to_signed_in_users_only(logged_in, portal, settings, biz):  # noqa: F811
    from app.database.connection import connect
    from app.services.conversation import get_or_create_customer

    conn = connect(settings.database_path)
    cid = get_or_create_customer(conn, "PAGE1", "PSID77")["id"]
    _design_cart(conn, cid)
    p = photos.save(conn, settings, cid, "m9", 0, JPEG)
    photos.attach_to_cart(conn, cid, [p.id])
    cart.set_pickup_details(conn, cid, pickup_time=PICKUP, customer_name="Jo", customer_phone="225-555-0123")
    s = order_svc.present_order_summary(conn, cid, biz, settings, NOW)
    order_svc.submit_order(conn, cid, s.token, biz, settings, NOW)
    r = logged_in.get(f"/admin/photos/{p.id}")
    assert r.status_code == 200 and r.content == JPEG and r.headers["x-content-type-options"] == "nosniff"
    assert f'/admin/photos/{p.id}' in logged_in.get("/admin/orders").text
    assert f'/admin/photos/{p.id}' in logged_in.get(f"/admin/customers/{cid}").text
    assert logged_in.get("/admin/photos/999").status_code == 404
    logged_in.cookies.clear()
    assert logged_in.get(f"/admin/photos/{p.id}", follow_redirects=False).status_code in (302, 303, 401)
    conn.close()


def test_portal_clears_wrong_link_codes(logged_in, settings):  # noqa: F811
    from app.database.connection import connect

    from app.timeutil import utcnow

    now = utcnow()  # the owners page reads the real clock
    conn = connect(settings.database_path)
    for i in range(5):
        with pytest.raises(owner_accounts.LinkError):
            owner_accounts.link(conn, "PAGE1", "OWNER9", f"WRONG{i}XX", now)
    assert "Clear wrong tries" in logged_in.get("/admin/owners").text
    r = logged_in.post("/admin/owners/link-lockout/clear", headers={"HX-Request": "true"})
    assert r.status_code == 204
    assert owner_accounts.lockout_status(conn, now)["wrong_codes"] == 0
    conn.close()

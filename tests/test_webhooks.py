import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app


@pytest.fixture
def client(settings, monkeypatch):
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    with TestClient(app) as c:
        yield c


def sign(body: bytes, secret="test-secret") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def payload(*events, page="PAGE1"):
    return json.dumps({"object": "page", "entry": [{"id": page, "time": 1, "messaging": list(events)}]}).encode()


def text_event(mid="m1", text="hi", psid="PSID1"):
    return {"sender": {"id": psid}, "recipient": {"id": "PAGE1"}, "timestamp": 1, "message": {"mid": mid, "text": text}}


def post(client, body, sig=None):
    return client.post("/webhooks/messenger", content=body,
                       headers={"X-Hub-Signature-256": sig or sign(body), "Content-Type": "application/json"})


def queued(settings):
    from app.database.connection import connect

    c = connect(settings.database_path)
    return c.execute("SELECT external_event_id, event_type, customer_key, status FROM webhook_events ORDER BY id").fetchall()


def test_verify_ok(client):
    r = client.get("/webhooks/messenger", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
    assert r.status_code == 200 and r.text == "42"


def test_verify_wrong_token(client):
    r = client.get("/webhooks/messenger", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"})
    assert r.status_code == 403


@pytest.mark.parametrize("sig", ["", "sha256=deadbeef", "sha1=abc"])
def test_bad_signature_rejected(client, settings, sig):
    body = payload(text_event())
    r = client.post("/webhooks/messenger", content=body, headers={"X-Hub-Signature-256": sig})
    assert r.status_code == 403
    assert queued(settings) == []


def test_signature_over_raw_body(client, settings):
    body = payload(text_event())
    tampered = body.replace(b"hi", b"yo")
    assert post(client, tampered, sig=sign(body)).status_code == 403


def test_event_persisted_and_deduplicated(client, settings):
    body = payload(text_event())
    assert post(client, body).text == "EVENT_RECEIVED"
    assert post(client, body).status_code == 200
    rows = queued(settings)
    assert len(rows) == 1
    assert tuple(rows[0]) == ("PAGE1:m1", "message", "PAGE1:PSID1", "pending")


def test_echo_and_postback_classified(client, settings):
    echo = {"sender": {"id": "PAGE1"}, "recipient": {"id": "PSID1"}, "timestamp": 2,
            "message": {"mid": "m2", "text": "hello", "is_echo": True, "app_id": 999}}
    pb = {"sender": {"id": "PSID1"}, "recipient": {"id": "PAGE1"}, "timestamp": 3,
          "postback": {"title": "Confirm", "payload": "CONFIRM_ORDER:x", "mid": "m3"}}
    read = {"sender": {"id": "PSID1"}, "recipient": {"id": "PAGE1"}, "timestamp": 4, "read": {"watermark": 1}}
    post(client, payload(echo, pb, read))
    rows = queued(settings)
    assert [(r["event_type"], r["customer_key"]) for r in rows] == [("echo", "PAGE1:PSID1"), ("postback", "PAGE1:PSID1")]


def test_other_page_ignored(client, settings):
    post(client, payload(text_event(), page="SOMEONE_ELSE"))
    assert queued(settings) == []


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_docs_not_exposed(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404

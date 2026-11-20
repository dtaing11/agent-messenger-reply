import re

import httpx
import pytest
from fastapi.testclient import TestClient

from app.admin import app as admin_mod
from app.config import get_settings
from app.database.connection import connect
from app.settings.runtime import effective_settings
from app.settings.secret_store import SecretStore

PASSWORD = "correct horse battery"


@pytest.fixture
def portal(settings, monkeypatch):
    monkeypatch.setattr(admin_mod, "get_settings", lambda: settings)
    app = admin_mod.create_admin_app()
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app, base_url="http://127.0.0.1:8001") as c:
        yield c


def code(settings):
    return (settings.database_path.parent / "bootstrap_code.txt").read_text().strip()


def csrf(html: str) -> str:
    return re.search(r'"X-CSRF-Token": "([^"]+)"', html).group(1)


@pytest.fixture
def logged_in(portal, settings):
    r = portal.post("/admin/setup", data={"code": code(settings), "username": "dina", "password": PASSWORD})
    assert r.status_code == 200 and "Account created" in r.text
    r = portal.post("/admin/login", data={"username": "dina", "password": PASSWORD})
    assert r.status_code == 200 and "Overview" in r.text
    portal.headers["X-CSRF-Token"] = csrf(r.text)
    return portal


def test_redirects_to_setup_then_login(portal, settings):
    assert portal.get("/admin/", follow_redirects=False).headers["location"] == "/admin/login"
    assert portal.get("/admin/login", follow_redirects=False).headers["location"] == "/admin/setup"


def test_bootstrap_code_required_and_one_time(portal, settings):
    r = portal.post("/admin/setup", data={"code": "WRONG-CODE-0000", "username": "dina", "password": PASSWORD})
    assert "wrong or expired" in r.text
    c = code(settings)
    portal.post("/admin/setup", data={"code": c, "username": "dina", "password": PASSWORD})
    assert not (settings.database_path.parent / "bootstrap_code.txt").exists()
    r = portal.post("/admin/setup", data={"code": c, "username": "evil", "password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"


def test_short_password_rejected(portal, settings):
    r = portal.post("/admin/setup", data={"code": code(settings), "username": "dina", "password": "short"})
    assert "at least 12" in r.text


def test_login_lockout(logged_in):
    logged_in.cookies.clear()
    for _ in range(5):
        assert "Wrong username" in logged_in.post("/admin/login", data={"username": "dina", "password": "nope"}).text
    r = logged_in.post("/admin/login", data={"username": "dina", "password": PASSWORD})
    assert "Too many failed attempts" in r.text


def test_security_headers(logged_in):
    r = logged_in.get("/admin/")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store" and r.headers["x-frame-options"] == "DENY"


def test_csrf_required(logged_in):
    del logged_in.headers["X-CSRF-Token"]
    r = logged_in.post("/admin/bot/mode", data={"mode": "paused"})
    assert r.status_code == 403


def test_save_meta_encrypts_and_applies(logged_in, settings):
    r = logged_in.post("/admin/integrations/meta", data={
        "meta_page_id": "PAGE9", "meta_app_id": "APP9", "meta_graph_api_version": "v21.0",
        "meta_app_secret": "s3cret-value", "meta_page_access_token": "", "public_base_url": "https://abc.trycloudflare.com/",
    })
    assert "Saved" in r.text and "Updated 1 credential" in r.text
    conn = connect(settings.database_path)
    raw = conn.execute("SELECT ciphertext FROM secret_versions WHERE name = 'meta_app_secret'").fetchone()[0]
    assert b"s3cret-value" not in raw
    eff = effective_settings(conn, settings, SecretStore.from_path(settings.master_key_path))
    assert eff.meta_page_id == "PAGE9" and eff.meta_app_secret.get_secret_value() == "s3cret-value"
    assert eff.public_base_url == "https://abc.trycloudflare.com"
    # The integrations page never shows the secret back.
    page = logged_in.get("/admin/integrations").text
    assert "s3cret-value" not in page and "https://abc.trycloudflare.com/webhooks/messenger" in page
    audit = " ".join(str(tuple(r)) for r in conn.execute("SELECT * FROM audit_events"))
    assert "s3cret-value" not in audit


def test_bad_public_url_rejected(logged_in):
    r = logged_in.post("/admin/integrations/meta", data={"public_base_url": "http://localhost:8000"})
    assert "https://" in r.text and "✗" in r.text


def test_verify_token_generate_and_reveal(logged_in):
    r = logged_in.post("/admin/integrations/meta/verify-token", data={"action": "generate"})
    token = re.search(r'<code id="vt">([^<]+)</code>', r.text).group(1)
    r2 = logged_in.post("/admin/integrations/meta/verify-token", data={"action": "reveal"})
    assert token in r2.text


def test_bot_mode_requires_app_secret(logged_in, settings):
    settings.meta_app_secret = settings.meta_app_secret.__class__("")
    r = logged_in.post("/admin/bot/mode", data={"mode": "live"})
    assert "App secret first" in r.text
    settings.meta_app_secret = settings.meta_app_secret.__class__("x")
    assert "now live" in logged_in.post("/admin/bot/mode", data={"mode": "live"}).text


def test_business_settings(logged_in, settings):
    r = logged_in.post("/admin/business", data={
        "tax_rate_percent": "0", "pending_order_expiry_minutes": "90", "large_order_threshold": "150",
        "owner_phone_numbers": "+12255550001\n+12255550002", "pause_bot_on_staff_reply": "on",
    })
    assert "Saved" in r.text
    eff = effective_settings(connect(settings.database_path), settings, None)
    assert eff.pending_order_expiry_minutes == 90 and eff.large_order_threshold_cents == 15000
    assert eff.owner_phone_numbers == ["+12255550001", "+12255550002"] and eff.customer_can_cancel_accepted is False


def test_pages_render(logged_in):
    for path in ("/admin/", "/admin/integrations", "/admin/business", "/admin/orders", "/admin/audit", "/admin/partials/health"):
        assert logged_in.get(path).status_code == 200, path
    assert logged_in.get("/admin/v1/overview").json()["bot_mode"] in ("live", "paused")


def _graph_transport(calls):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.host, request.url.path))
        if request.url.host == "abc.trycloudflare.com":
            return httpx.Response(200, text=request.url.params["hub.challenge"])
        if request.url.path.endswith("/subscriptions") or request.url.path.endswith("/subscribed_apps"):
            return httpx.Response(200, json={"success": True})
        if request.url.path.endswith("/me"):
            return httpx.Response(200, json={"id": "PAGE1", "name": "Town Donut"})
        return httpx.Response(200, json={"id": "APP1", "name": "Town Donut Bot"})
    return httpx.MockTransport(handler)


def test_connect_meta_automatically(logged_in, settings):
    settings.meta_page_access_token = settings.meta_page_access_token.__class__("page-token")
    logged_in.post("/admin/integrations/meta", data={"public_base_url": "https://abc.trycloudflare.com"})
    calls = []
    logged_in.app.state.http = httpx.AsyncClient(transport=_graph_transport(calls))
    r = logged_in.post("/admin/integrations/meta/connect")
    assert "Connected" in r.text, r.text
    assert ("POST", "graph.facebook.com", "/v21.0/APP1/subscriptions") in calls
    assert ("POST", "graph.facebook.com", "/v21.0/PAGE1/subscribed_apps") in calls
    r = logged_in.post("/admin/integrations/meta/test")
    assert "Meta credentials work" in r.text


def test_connect_stops_if_tunnel_unreachable(logged_in, settings):
    settings.meta_page_access_token = settings.meta_page_access_token.__class__("page-token")
    logged_in.post("/admin/integrations/meta", data={"public_base_url": "https://abc.trycloudflare.com"})
    calls = []
    logged_in.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: (calls.append(req.url.host), httpx.Response(502))[1]))
    r = logged_in.post("/admin/integrations/meta/connect")
    assert "isn&#39;t reachable" in r.text and calls == ["abc.trycloudflare.com"]


def test_public_app_has_no_admin_routes(settings, monkeypatch):
    from app.main import create_app

    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    with TestClient(create_app()) as c:
        assert c.get("/admin/").status_code == 404
        assert c.get("/admin/v1/overview").status_code == 404

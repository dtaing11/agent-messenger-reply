"""Connection tests and the automatic Meta webhook connection, used by the portal.

All outbound calls go to fixed provider hosts. The only user-supplied URL is the public
base URL, which must be HTTPS on a real hostname (no IPs or localhost), to avoid SSRF.
Error details are sanitized so tokens never end up in the UI or audit log.
"""

import ipaddress
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.config import Settings

GRAPH = "https://graph.facebook.com"
MESSENGER_FIELDS = "messages,messaging_postbacks,message_echoes"


@dataclass
class CheckResult:
    ok: bool
    status: str  # HEALTHY | DOWN | UNCONFIGURED
    detail: str
    latency_ms: int | None = None
    steps: list[tuple[str, bool, str]] | None = None


def _graph_error(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error", {})
        return f"{err.get('type', 'Error')}: {err.get('message', resp.reason_phrase)}"[:300]
    except ValueError:
        return f"HTTP {resp.status_code}"


def validate_public_base_url(url: str) -> str:
    url = url.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/") or parsed.query:
        raise ValueError("Use the bare https:// address of your tunnel, e.g. https://orders.example.com")
    host = parsed.hostname
    if host == "localhost" or host.endswith(".local"):
        raise ValueError("The public address can't be localhost; Meta needs to reach it from the internet.")
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    if is_ip:
        raise ValueError("Use a hostname, not an IP address.")
    return f"https://{host}" + (f":{parsed.port}" if parsed.port else "")


def callback_url(settings: Settings) -> str | None:
    return f"{settings.public_base_url.rstrip('/')}/webhooks/messenger" if settings.public_base_url else None


def meta_missing(settings: Settings) -> list[str]:
    need = {
        "Page ID": settings.meta_page_id,
        "App ID": settings.meta_app_id,
        "App secret": settings.meta_app_secret.get_secret_value(),
        "Page access token": settings.meta_page_access_token.get_secret_value(),
        "Verify token": settings.meta_verify_token.get_secret_value(),
    }
    return [k for k, v in need.items() if not v]


async def test_meta(settings: Settings, http: httpx.AsyncClient) -> CheckResult:
    missing = [m for m in meta_missing(settings) if m != "Verify token"]
    if missing:
        return CheckResult(False, "UNCONFIGURED", "Missing: " + ", ".join(missing))
    v = settings.meta_graph_api_version
    start = time.monotonic()
    steps = []
    app_token = f"{settings.meta_app_id}|{settings.meta_app_secret.get_secret_value()}"
    r = await http.get(f"{GRAPH}/{v}/{settings.meta_app_id}", params={"fields": "id,name"},
                       headers={"Authorization": f"Bearer {app_token}"})
    steps.append(("App ID and App secret", r.status_code == 200, r.json().get("name", "") if r.status_code == 200 else _graph_error(r)))
    page_token = settings.meta_page_access_token.get_secret_value()
    r = await http.get(f"{GRAPH}/{v}/me", params={"fields": "id,name"}, headers={"Authorization": f"Bearer {page_token}"})
    if r.status_code == 200:
        data = r.json()
        same = str(data.get("id")) == settings.meta_page_id
        steps.append(("Page access token", same, f"{data.get('name', '')} ({data.get('id')})" if same
                      else f"Token belongs to {data.get('id')}, not Page ID {settings.meta_page_id}"))
    else:
        steps.append(("Page access token", False, _graph_error(r)))
    ok = all(s[1] for s in steps)
    detail = "Meta credentials work." if ok else "; ".join(f"{n}: {d}" for n, good, d in steps if not good)
    return CheckResult(ok, "HEALTHY" if ok else "DOWN", detail, int((time.monotonic() - start) * 1000), steps)


async def connect_meta(settings: Settings, http: httpx.AsyncClient) -> CheckResult:
    """Point the Meta app's Page webhook at our callback URL and subscribe the Page.

    1. Check the callback is reachable from the internet with our verify token.
    2. POST /{app-id}/subscriptions (app token): Meta calls our GET verification right away.
    3. POST /{page-id}/subscribed_apps (Page token): the Page starts sending us messages.
    """
    missing = meta_missing(settings)
    if not settings.public_base_url:
        missing.append("Public URL")
    if missing:
        return CheckResult(False, "UNCONFIGURED", "Missing: " + ", ".join(missing))
    url = callback_url(settings)
    verify = settings.meta_verify_token.get_secret_value()
    v = settings.meta_graph_api_version
    steps: list[tuple[str, bool, str]] = []
    start = time.monotonic()

    try:
        r = await http.get(url, params={"hub.mode": "subscribe", "hub.verify_token": verify, "hub.challenge": "td-check"})
        reachable = r.status_code == 200 and r.text == "td-check"
        steps.append(("Callback URL reachable", reachable, url if reachable else f"HTTP {r.status_code} from {url}"))
    except httpx.HTTPError as exc:
        steps.append(("Callback URL reachable", False, f"{type(exc).__name__} for {url}"))
    if not steps[-1][1]:
        return CheckResult(False, "DOWN", "Your public URL isn't reachable. Is the tunnel running?", None, steps)

    app_token = f"{settings.meta_app_id}|{settings.meta_app_secret.get_secret_value()}"
    r = await http.post(
        f"{GRAPH}/{v}/{settings.meta_app_id}/subscriptions",
        data={"object": "page", "callback_url": url, "verify_token": verify, "fields": MESSENGER_FIELDS, "include_values": "true"},
        headers={"Authorization": f"Bearer {app_token}"},
    )
    steps.append(("App webhook set to callback URL", r.status_code == 200, "Meta verified the callback" if r.status_code == 200 else _graph_error(r)))

    if steps[-1][1]:
        r = await http.post(
            f"{GRAPH}/{v}/{settings.meta_page_id}/subscribed_apps",
            data={"subscribed_fields": MESSENGER_FIELDS},
            headers={"Authorization": f"Bearer {settings.meta_page_access_token.get_secret_value()}"},
        )
        steps.append(("Page subscribed to messages", r.status_code == 200, MESSENGER_FIELDS if r.status_code == 200 else _graph_error(r)))

    ok = all(s[1] for s in steps)
    detail = "Connected. Messages to your Page now reach this server." if ok else next(d for n, g, d in steps if not g)
    return CheckResult(ok, "HEALTHY" if ok else "DOWN", detail, int((time.monotonic() - start) * 1000), steps)


async def test_openrouter(settings: Settings, http: httpx.AsyncClient) -> CheckResult:
    key = settings.openrouter_api_key.get_secret_value()
    if not key:
        return CheckResult(False, "UNCONFIGURED", "Missing: API key")
    start = time.monotonic()
    r = await http.get("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key}"})
    if r.status_code != 200:
        return CheckResult(False, "DOWN", f"API key rejected (HTTP {r.status_code})", int((time.monotonic() - start) * 1000))
    detail = "API key works."
    if settings.ai_model:
        m = await http.get("https://openrouter.ai/api/v1/models")
        ids = {x.get("id") for x in m.json().get("data", [])} if m.status_code == 200 else set()
        if ids and settings.ai_model not in ids:
            return CheckResult(False, "DOWN", f"Model '{settings.ai_model}' isn't available on OpenRouter.",
                               int((time.monotonic() - start) * 1000))
        detail += f" Model {settings.ai_model} is available."
    return CheckResult(True, "HEALTHY", detail, int((time.monotonic() - start) * 1000))


async def test_twilio(settings: Settings, http: httpx.AsyncClient) -> CheckResult:
    sid, token = settings.twilio_account_sid, settings.twilio_auth_token.get_secret_value()
    missing = [n for n, v in (("Account SID", sid), ("Auth token", token), ("Sending number", settings.twilio_phone_number)) if not v]
    if not settings.owner_phone_numbers:
        missing.append("Owner phone numbers")
    if missing:
        return CheckResult(False, "UNCONFIGURED", "Missing: " + ", ".join(missing))
    start = time.monotonic()
    r = await http.get(f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json", auth=(sid, token))
    ms = int((time.monotonic() - start) * 1000)
    if r.status_code != 200:
        return CheckResult(False, "DOWN", f"Twilio rejected the credentials (HTTP {r.status_code})", ms)
    return CheckResult(True, "HEALTHY", f"Account '{r.json().get('friendly_name', sid)}' is reachable. No SMS was sent.", ms)


async def detect_quick_tunnel(ports: list[int], http: httpx.AsyncClient) -> str | None:
    """Read the current trycloudflare.com hostname from cloudflared's local metrics server."""
    for port in ports:
        try:
            r = await http.get(f"http://127.0.0.1:{port}/quicktunnel", timeout=1.5)
        except httpx.HTTPError:
            continue
        if r.status_code == 200 and (host := r.json().get("hostname")):
            return f"https://{host}"
    return None

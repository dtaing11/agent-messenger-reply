"""Twilio SMS over its REST API (httpx), plus webhook signature validation.

Docs: https://www.twilio.com/docs/messaging/api/message-resource and
https://www.twilio.com/docs/usage/security#validating-requests
"""

import base64
import hashlib
import hmac
import re
from collections.abc import Iterable

import httpx

from app.config import Settings

API = "https://api.twilio.com/2010-04-01"
_shared: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _shared
    if _shared is None:
        _shared = httpx.AsyncClient(timeout=15.0)
    return _shared


class SMSError(Exception):
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def configured(settings: Settings) -> bool:
    return bool(settings.twilio_account_sid and settings.twilio_auth_token.get_secret_value() and settings.twilio_phone_number)


def digits(phone: str) -> str:
    """Last 10 digits, so +1 (225) 555-0100 and 2255550100 compare equal."""
    return re.sub(r"\D", "", phone or "")[-10:]


def is_owner(settings: Settings, phone: str) -> bool:
    d = digits(phone)
    return bool(d) and any(digits(p) == d for p in settings.owner_phone_numbers)


async def send_sms(settings: Settings, to: str, body: str, status_callback: str | None = None,
                   http: httpx.AsyncClient | None = None) -> str:
    """Send one SMS and return Twilio's message SID."""
    if not configured(settings):
        raise SMSError("Twilio is not set up yet", retryable=True)
    data = {"To": to, "From": settings.twilio_phone_number, "Body": body[:1500]}
    if status_callback:
        data["StatusCallback"] = status_callback
    try:
        resp = await (http or _client()).post(
            f"{API}/Accounts/{settings.twilio_account_sid}/Messages.json",
            data=data,
            auth=(settings.twilio_account_sid, settings.twilio_auth_token.get_secret_value()),
        )
    except httpx.HTTPError as exc:
        raise SMSError(f"network: {type(exc).__name__}") from exc
    if resp.status_code in (200, 201):
        return resp.json().get("sid", "")
    try:
        err = resp.json()
        msg = f"HTTP {resp.status_code} code {err.get('code')}: {err.get('message', '')}"
    except ValueError:
        msg = f"HTTP {resp.status_code}"
    # 4xx (bad number, unverified trial recipient, bad credentials) won't fix itself; 429/5xx might.
    raise SMSError(msg[:300], retryable=resp.status_code == 429 or resp.status_code >= 500)


def expected_signature(auth_token: str, url: str, params: Iterable[tuple[str, str]]) -> str:
    """X-Twilio-Signature: base64(HMAC-SHA1(token, url + each param name+value, sorted by name))."""
    data = url + "".join(k + v for k, v in sorted(params))
    mac = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode()


def valid_signature(auth_token: str, url: str, params: Iterable[tuple[str, str]], signature: str | None) -> bool:
    if not auth_token or not signature:
        return False
    return hmac.compare_digest(expected_signature(auth_token, url, params), signature)


async def point_number_at(settings: Settings, sms_url: str, http: httpx.AsyncClient | None = None) -> str:
    """Set the Twilio number's incoming-SMS webhook to `sms_url`. Returns a short status."""
    if not configured(settings):
        raise SMSError("Save the Account SID, Auth token and Twilio phone number first.", retryable=False)
    auth = (settings.twilio_account_sid, settings.twilio_auth_token.get_secret_value())
    client = http or _client()
    base = f"{API}/Accounts/{settings.twilio_account_sid}/IncomingPhoneNumbers"
    resp = await client.get(f"{base}.json", params={"PhoneNumber": settings.twilio_phone_number}, auth=auth)
    if resp.status_code != 200:
        raise SMSError(f"Couldn't look up the number (HTTP {resp.status_code}).", retryable=False)
    numbers = resp.json().get("incoming_phone_numbers", [])
    if not numbers:
        raise SMSError("That phone number isn't on this Twilio account. Check the number format, like +12255551234.",
                       retryable=False)
    sid = numbers[0]["sid"]
    resp = await client.post(f"{base}/{sid}.json", data={"SmsUrl": sms_url, "SmsMethod": "POST"}, auth=auth)
    if resp.status_code != 200:
        raise SMSError(f"Couldn't update the number (HTTP {resp.status_code}).", retryable=False)
    return f"Texts to {settings.twilio_phone_number} now go to {sms_url}"

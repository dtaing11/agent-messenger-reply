"""Meta Send API client. Not exercised live yet (no Meta app exists); covered by mocked tests."""

import httpx

from app.config import Settings

POSTBACK_CONFIRM = "CONFIRM_ORDER:"
POSTBACK_MODIFY = "MODIFY_ORDER"
PICK = "PICK:"  # quick-reply option chosen by tapping; the worker treats it as the customer's text
QUICK_REPLY_TITLE_MAX = 20  # Messenger's limit for a quick-reply button title


MAX_TEXT = 2000  # Messenger's limit per message


def split_message(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Split long text into Messenger-sized parts at line (then word) boundaries,
    so nothing at the end of a long summary (like the pickup location) is cut off."""
    parts: list[str] = []
    cur = ""
    for line in text.split("\n"):
        while len(line) > limit:
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:cut])
            line = line[cut:].lstrip()
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate) > limit:
            parts.append(cur)
            cur = line
        else:
            cur = candidate
    if cur.strip() or not parts:
        parts.append(cur)
    return [p.strip("\n") for p in parts if p.strip()] or [""]


_shared_http: httpx.AsyncClient | None = None


def _http_client() -> httpx.AsyncClient:
    global _shared_http
    if _shared_http is None:
        _shared_http = httpx.AsyncClient(timeout=10.0)
    return _shared_http


class MessengerClient:
    """Cheap to construct per job (pins that job's settings); shares one HTTP connection pool."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self._settings = settings
        self._http = http or _http_client()

    @property
    def _url(self) -> str:
        return f"https://graph.facebook.com/{self._settings.meta_graph_api_version}/me/messages"

    async def _send(self, payload: dict) -> dict:
        token = self._settings.meta_page_access_token.get_secret_value()
        # Token goes in a header, never in a logged URL.
        resp = await self._http.post(self._url, json=payload, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        return resp.json()

    async def send_text(self, psid: str, text: str, messaging_type: str = "RESPONSE", tag: str | None = None) -> dict:
        result: dict = {}
        for part in split_message(text):
            payload = {"recipient": {"id": psid}, "messaging_type": messaging_type, "message": {"text": part}}
            if tag:
                payload["tag"] = tag
            result = await self._send(payload)
        return result

    async def send_choices(self, psid: str, text: str, options: list[str] | tuple[str, ...]) -> dict:
        """Reply text with tap-to-answer buttons (up to 13). The button shows a short title; the
        payload carries the full option, which the worker reads as if the customer typed it."""
        *head, last = split_message(text)
        for part in head:
            await self._send({"recipient": {"id": psid}, "messaging_type": "RESPONSE", "message": {"text": part}})
        return await self._send({
            "recipient": {"id": psid},
            "messaging_type": "RESPONSE",
            "message": {"text": last, "quick_replies": quick_replies(options)},
        })

    async def send_update(self, psid: str, text: str) -> dict:
        """A message the customer didn't just ask for (order accepted/ready). Allowed within
        24 hours of their last message."""
        return await self.send_text(psid, text, messaging_type="UPDATE")

    async def send_order_summary(self, psid: str, summary_text: str, token: str) -> dict:
        """Summary with Confirm / Modify buttons. The confirm payload carries the token,
        which the worker validates deterministically (no LLM involved)."""
        *head, last = split_message(summary_text)
        for part in head:
            await self._send({"recipient": {"id": psid}, "messaging_type": "RESPONSE", "message": {"text": part}})
        return await self._send({
            "recipient": {"id": psid},
            "messaging_type": "RESPONSE",
            "message": {
                "text": last,
                "quick_replies": [
                    {"content_type": "text", "title": "Confirm Order", "payload": POSTBACK_CONFIRM + token},
                    {"content_type": "text", "title": "Modify", "payload": POSTBACK_MODIFY},
                ],
            },
        })


def quick_replies(options: list[str] | tuple[str, ...]) -> list[dict]:
    out = []
    for o in list(options)[:13]:
        title = o if len(o) <= QUICK_REPLY_TITLE_MAX else o[: QUICK_REPLY_TITLE_MAX - 1].rstrip() + "…"
        out.append({"content_type": "text", "title": title, "payload": PICK + o[:900]})
    return out

"""Meta Send API client. Not exercised live yet (no Meta app exists); covered by mocked tests."""

import httpx

from app.config import Settings

POSTBACK_CONFIRM = "CONFIRM_ORDER:"
POSTBACK_MODIFY = "MODIFY_ORDER"


class MessengerClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None):
        self._settings = settings
        self._http = http or httpx.AsyncClient(timeout=10.0)

    @property
    def _url(self) -> str:
        return f"https://graph.facebook.com/{self._settings.meta_graph_api_version}/me/messages"

    async def _send(self, payload: dict) -> dict:
        token = self._settings.meta_page_access_token.get_secret_value()
        # Token goes in a header, never in a logged URL.
        resp = await self._http.post(self._url, json=payload, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        return resp.json()

    async def send_text(self, psid: str, text: str) -> dict:
        return await self._send({"recipient": {"id": psid}, "messaging_type": "RESPONSE", "message": {"text": text[:2000]}})

    async def send_order_summary(self, psid: str, summary_text: str, token: str) -> dict:
        """Summary with Confirm / Modify buttons. The confirm payload carries the token,
        which the worker validates deterministically (no LLM involved)."""
        return await self._send({
            "recipient": {"id": psid},
            "messaging_type": "RESPONSE",
            "message": {
                "text": summary_text[:2000],
                "quick_replies": [
                    {"content_type": "text", "title": "Confirm Order", "payload": POSTBACK_CONFIRM + token},
                    {"content_type": "text", "title": "Modify", "payload": POSTBACK_MODIFY},
                ],
            },
        })

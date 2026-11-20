"""OpenRouter chat completions (OpenAI-compatible) over httpx.

Each call uses the settings snapshot of the job it belongs to, so a key or model changed
in the portal applies to the next message without a restart.
"""

import httpx

from app.config import Settings

URL = "https://openrouter.ai/api/v1/chat/completions"
_shared: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    global _shared
    if _shared is None:
        _shared = httpx.AsyncClient()
    return _shared


class LLMError(Exception):
    pass


def request_body(settings: Settings, model: str, messages: list[dict], tools: list[dict] | None) -> dict:
    body: dict = {
        "model": model,
        "messages": messages,
        "temperature": settings.ai_temperature,
        "max_tokens": settings.ai_max_tokens,
        "provider": {"require_parameters": True},
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if settings.ai_deny_data_collection:
        body["provider"]["data_collection"] = "deny"
    return body


async def chat(settings: Settings, messages: list[dict], tools: list[dict] | None = None,
               model: str | None = None, http: httpx.AsyncClient | None = None) -> dict:
    """Return the assistant message dict (content and/or tool_calls)."""
    key = settings.openrouter_api_key.get_secret_value()
    model = model or settings.ai_model
    if not key or not model:
        raise LLMError("OpenRouter API key or model is not configured")
    resp = await (http or _client()).post(
        URL,
        json=request_body(settings, model, messages, tools),
        headers={"Authorization": f"Bearer {key}", "X-Title": "Town Donut ordering assistant"},
        timeout=settings.ai_timeout_seconds,
    )
    if resp.status_code != 200:
        try:
            msg = resp.json().get("error", {}).get("message", "")
        except ValueError:
            msg = ""
        raise LLMError(f"{model}: HTTP {resp.status_code} {msg}"[:300])
    data = resp.json()
    if "error" in data:
        raise LLMError(f"{model}: {data['error'].get('message', 'error')}"[:300])
    try:
        return data["choices"][0]["message"]
    except (KeyError, IndexError) as exc:
        raise LLMError(f"{model}: malformed response") from exc


async def chat_with_fallback(settings: Settings, messages: list[dict], tools: list[dict] | None = None,
                             http: httpx.AsyncClient | None = None) -> dict:
    try:
        return await chat(settings, messages, tools, settings.ai_model, http)
    except (LLMError, httpx.HTTPError) as first:
        if not settings.ai_fallback_model:
            raise LLMError(str(first)) from first
        try:
            return await chat(settings, messages, tools, settings.ai_fallback_model, http)
        except (LLMError, httpx.HTTPError) as second:
            raise LLMError(f"{first} | fallback: {second}") from second

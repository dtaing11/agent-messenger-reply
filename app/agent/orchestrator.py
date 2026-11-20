"""The ordering assistant's tool loop.

One customer message in, one AgentResult out: a reply text, an order summary to send with
Confirm/Modify buttons, and/or a request to hand the conversation to a person. The model
only ever acts through app.agent.tools, scoped to the customer of the current event.
"""

import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from app.agent import tools
from app.agent.prompts import system_prompt
from app.config import Settings
from app.integrations.openrouter import LLMError, chat_with_fallback
from app.services import conversation as conv_svc
from app.services.business import BusinessInfo
from app.services.order import OrderSummary
from app.timeutil import local

log = logging.getLogger(__name__)

LLM = Callable[[Settings, list[dict], list[dict] | None], Awaitable[dict]]

TOO_MANY_STEPS = "Sorry, I got a bit mixed up there. I've asked someone from our team to help, and they'll reply here soon."
HANDOFF_DEFAULT = "Thanks! I've passed this to our team, and someone will reply here soon."


@dataclass
class AgentResult:
    text: str | None = None
    summary: OrderSummary | None = None
    handoff_reason: str | None = None
    error: str | None = None  # the LLM failed; the caller apologizes and hands off
    tools_used: tuple[str, ...] = ()


def ai_configured(settings: Settings) -> bool:
    return bool(settings.openrouter_api_key.get_secret_value() and settings.ai_model)


def _history(conn: sqlite3.Connection, customer_id: int, limit: int) -> list[dict]:
    out: list[dict] = []
    for m in conv_svc.recent_messages(conn, customer_id, limit):
        if m["role"] == "customer":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            out.append({"role": "assistant", "content": m["content"]})
        elif m["role"] == "staff":
            out.append({"role": "assistant", "content": f"(Store team, written by hand) {m['content']}"})
    # Providers expect the conversation to start with a user turn.
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _cart_note(ctx: tools.ToolContext) -> str:
    cart = tools._cart_json(ctx)
    if not cart["lines"] and not cart["pickup_time"] and not cart["pickup_name"]:
        return "The customer's cart is empty."
    return "Current cart (already saved, don't add these again): " + json.dumps(cart, ensure_ascii=False)


async def run_agent(
    conn: sqlite3.Connection,
    customer_id: int,
    settings: Settings,
    biz: BusinessInfo,
    now: datetime,
    llm: LLM | None = None,
) -> AgentResult:
    llm = llm or chat_with_fallback
    ctx = tools.ToolContext(conn, customer_id, settings, biz, now)
    messages: list[dict] = [
        {"role": "system", "content": system_prompt(biz, local(now, biz.timezone))},
        {"role": "system", "content": _cart_note(ctx)},
        *_history(conn, customer_id, settings.ai_history_messages),
    ]
    for _ in range(settings.max_tool_rounds):
        try:
            msg = await llm(settings, messages, tools.TOOLS)
        except LLMError as exc:
            log.warning("LLM failed: %s", exc)
            return AgentResult(error=str(exc), tools_used=tuple(ctx.log))
        calls = msg.get("tool_calls") or []
        content = (msg.get("content") or "").strip()
        if not calls:
            return _finish(ctx, content)
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            fn = call.get("function") or {}
            result = await tools.execute(ctx, fn.get("name", ""), fn.get("arguments") or "{}")
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": json.dumps(result, ensure_ascii=False)})
        if ctx.summary is not None:
            # The summary itself is the reply; any text the model wrote alongside goes first.
            return _finish(ctx, content)
    log.warning("agent hit max_tool_rounds for customer %s", customer_id)
    return AgentResult(text=TOO_MANY_STEPS, handoff_reason="assistant could not finish the request",
                       tools_used=tuple(ctx.log))


def _finish(ctx: tools.ToolContext, content: str) -> AgentResult:
    text = content or None
    if ctx.handoff_reason and not text:
        text = HANDOFF_DEFAULT
    if text is None and ctx.summary is None:
        text = "Sorry, could you say that another way?"
    return AgentResult(text=text, summary=ctx.summary, handoff_reason=ctx.handoff_reason, tools_used=tuple(ctx.log))

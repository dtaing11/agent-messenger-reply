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

from app.agent import reply_check, tools
from app.agent.prompts import system_prompt
from app.config import Settings
from app.integrations.openrouter import LLMError, chat_with_fallback
from app.services import conversation as conv_svc
from app.services import sessions
from app.services.business import BusinessInfo
from app.services.order import OrderSummary
from app.timeutil import local

log = logging.getLogger(__name__)

LLM = Callable[[Settings, list[dict], list[dict] | None], Awaitable[dict]]

TOO_MANY_STEPS = "Sorry, I got a bit mixed up there. I've asked someone from our team to help, and they'll reply here soon."
HANDOFF_DEFAULT = "Thanks! I've passed this to our team, and someone will reply here soon."
NOT_UNDERSTOOD = "Sorry, could you say that another way?"
ASK_AGAIN = "Sorry, I didn't catch that."
EMPTY_NUDGE = ("Your last reply was empty. Answer the customer's latest message now: write a short reply, or call a tool. "
               "If they tapped one of your buttons, their message is that button's text; treat it as their answer.")
# Saved with an assistant reply in history so the model knows which buttons the customer could tap.
BUTTONS_NOTE = "[Buttons shown: "


def with_buttons_note(text: str, options: tuple[str, ...] | list[str]) -> str:
    return f"{text}\n{BUTTONS_NOTE}{' | '.join(options)}]" if options else text


def split_buttons_note(content: str) -> tuple[str, list[str]]:
    """(text to send, button options) from a saved assistant reply."""
    head, sep, tail = content.rpartition("\n" + BUTTONS_NOTE)
    if not sep or not tail.endswith("]"):
        return content, []
    return head, [o for o in tail[:-1].split(" | ") if o]


def _strip_notes(text: str) -> str:
    """The model sometimes copies the history note into its reply; customers must never see it."""
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith(BUTTONS_NOTE)).strip()


@dataclass
class AgentResult:
    text: str | None = None
    summary: OrderSummary | None = None
    handoff_reason: str | None = None
    error: str | None = None  # the LLM failed; the caller apologizes and hands off
    tools_used: tuple[str, ...] = ()
    quick_replies: tuple[str, ...] = ()  # tap-to-answer options sent under the reply text


def ai_configured(settings: Settings) -> bool:
    return bool(settings.openrouter_api_key.get_secret_value() and settings.ai_model)


def _history(conn: sqlite3.Connection, customer_id: int, limit: int) -> list[dict]:
    """The latest messages of the current session (see services.sessions)."""
    conv = conv_svc.get_conversation(conn, customer_id)
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE conversation_id = ? AND id >= ? ORDER BY id DESC LIMIT ?",
        (conv["id"], conv["session_start_message_id"] or 0, limit),
    ).fetchall()
    out: list[dict] = []
    for m in reversed(rows):
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
        note = "The customer's cart is empty."
    else:
        note = "Current cart (already saved, don't add these again): " + json.dumps(cart, ensure_ascii=False)
    aside = sessions.set_aside_cart(ctx.conn, ctx.customer_id, ctx.settings.tax_rate_bps)
    if aside is not None and not aside.is_empty:
        items = [f"{l.quantity} x {l.name}" + (f" ({', '.join(str(v) for v in l.details.values())})" if l.details else "")
                 for l in aside.lines]
        note += ("\nThe customer started an order earlier and didn't finish it: " + "; ".join(items) + ". "
                 "It was set aside because they were away a while. Ask once, briefly, whether they'd like to pick it back up, "
                 "then call resume_unfinished_order with action restore or discard. Don't add those items yourself.")
    return note


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
    nudged = rechecked = False
    for _ in range(settings.max_tool_rounds):
        try:
            msg = await llm(settings, messages, tools.TOOLS)
        except LLMError as exc:
            log.warning("LLM failed: %s", exc)
            return AgentResult(error=str(exc), tools_used=tuple(ctx.log))
        except Exception as exc:  # noqa: BLE001 - never crash the reply part way (the worker undoes the cart)
            log.exception("LLM call failed unexpectedly")
            return AgentResult(error=f"{type(exc).__name__}: {exc}"[:200], tools_used=tuple(ctx.log))
        if not isinstance(msg, dict):
            return AgentResult(error="malformed AI response", tools_used=tuple(ctx.log))
        calls = msg.get("tool_calls") or []
        content = _strip_notes(msg.get("content") or "")
        if not calls and not content and not nudged and ctx.summary is None and not ctx.handoff_reason:
            # An empty answer (seen after a tapped button; often the token limit spent on reasoning):
            # ask once more instead of telling the customer we didn't understand.
            log.warning("empty LLM reply for customer %s (finish_reason=%s); asking again",
                        customer_id, msg.get("finish_reason"))
            nudged = True
            messages.append({"role": "system", "content": EMPTY_NUDGE})
            continue
        if not calls and content:
            issues = reply_check.problems(conn, customer_id, content)
            if issues and not rechecked:
                # One rewrite for a reply that promises a discount or a confirmed order.
                log.warning("reply for customer %s broke a rule (%s); asking for a rewrite", customer_id, ", ".join(issues))
                rechecked = True
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "system", "content": reply_check.rewrite_note(issues)})
                continue
            if issues:
                log.warning("rewrite for customer %s still broke a rule (%s); sending a safe reply", customer_id, ", ".join(issues))
                content = reply_check.fallback(issues)
                ctx.quick_replies = ()
            reply_check.unknown_amounts(conn, content, messages)
        if not calls:
            return _finish(ctx, content, messages)
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            fn = call.get("function") or {}
            result = await tools.execute(ctx, fn.get("name", ""), fn.get("arguments") or "{}")
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": json.dumps(result, ensure_ascii=False)})
        if ctx.summary is not None:
            # The summary itself is the reply; any text the model wrote alongside goes first.
            if content and reply_check.problems(conn, customer_id, content):
                content = ""  # the summary says everything that's true
            return _finish(ctx, content, messages)
    log.warning("agent hit max_tool_rounds for customer %s", customer_id)
    return AgentResult(text=TOO_MANY_STEPS, handoff_reason="assistant could not finish the request",
                       tools_used=tuple(ctx.log))


def _last_question(messages: list[dict]) -> tuple[str, list[str]] | None:
    """Sprinkle's latest reply in this session and its buttons, to ask again."""
    for m in reversed(messages):
        if m.get("role") == "assistant" and m.get("content") and not m.get("tool_calls"):
            text, options = split_buttons_note(str(m["content"]))
            if text.startswith(ASK_AGAIN):
                text = text.removeprefix(ASK_AGAIN).strip()
            return (text, options) if text.rstrip().endswith("?") else None
    return None


def _finish(ctx: tools.ToolContext, content: str, messages: list[dict] | None = None) -> AgentResult:
    text = content or None
    if ctx.handoff_reason and not text:
        text = HANDOFF_DEFAULT
    if text is None and ctx.summary is None:
        # Still nothing after a second try: repeat the last question (and its buttons) rather
        # than a bare "say that another way".
        again = _last_question(messages or [])
        if again:
            text = f"{ASK_AGAIN} {again[0]}"
            ctx.quick_replies = tuple(again[1])
        else:
            text = NOT_UNDERSTOOD
    # Buttons only make sense under a question; a summary brings its own, and a handoff ends the chat.
    quick = ctx.quick_replies if text and ctx.summary is None and not ctx.handoff_reason else ()
    return AgentResult(text=text, summary=ctx.summary, handoff_reason=ctx.handoff_reason, tools_used=tuple(ctx.log),
                       quick_replies=quick)

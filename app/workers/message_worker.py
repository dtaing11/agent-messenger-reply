"""Durable message worker: turns queued Messenger events into state changes and replies.

Run with:  python -m app.workers.message_worker
"""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.agent.orchestrator import AgentResult, ai_configured, run_agent
from app.agent.state_machine import ConversationState, OrderStatus
from app.config import Settings, get_settings
from app.database.bootstrap import prepare
from app.database.connection import connect
from app.integrations.messenger import POSTBACK_CONFIRM, POSTBACK_MODIFY, MessengerClient
from app.services import conversation as conv_svc
from app.services import notify, owner_commands
from app.services import order as order_svc
from app.services.business import BusinessInfo, load_business
from app.settings.runtime import effective_settings, set_marker
from app.settings.secret_store import SecretStore
from app.timeutil import to_db, utcnow
from app.workers import notifications, queue

log = logging.getLogger(__name__)


class Responder(Protocol):
    async def send_text(self, psid: str, text: str) -> object: ...
    async def send_order_summary(self, psid: str, summary_text: str, token: str) -> object: ...
    async def send_update(self, psid: str, text: str) -> object: ...


# Given the customer (whose latest message is already in history), produce what to send.
AgentHandler = Callable[[sqlite3.Connection, int, Settings, BusinessInfo, datetime], Awaitable[AgentResult | str | None]]

AI_DOWN_REPLY = "Sorry, I'm having trouble right now. I've let our team know, and someone will reply here soon."


async def placeholder_agent(conn: sqlite3.Connection, customer_id: int, settings: Settings, biz: BusinessInfo,
                            now: datetime) -> str | None:
    return settings.placeholder_reply


async def default_agent(conn: sqlite3.Connection, customer_id: int, settings: Settings, biz: BusinessInfo,
                        now: datetime) -> AgentResult | str | None:
    """The AI assistant once an OpenRouter key and model are saved; the placeholder until then."""
    if ai_configured(settings):
        return await run_agent(conn, customer_id, settings, biz, now)
    return settings.placeholder_reply


@dataclass(frozen=True)
class _Ctx:
    """Everything one event is processed with: a pinned settings snapshot and its client."""

    conn: sqlite3.Connection
    settings: Settings
    responder: Responder
    customer_id: int
    psid: str
    now: datetime


class MessageProcessor:
    def __init__(
        self,
        settings: Settings,
        biz: BusinessInfo,
        responder: Responder | Callable[[Settings], Responder],
        agent: AgentHandler | None = None,
        secret_store: SecretStore | None = None,
    ):
        self.base_settings = settings
        self.biz = biz
        self._responder_factory = responder if callable(responder) and not hasattr(responder, "send_text") else (lambda s: responder)
        self.agent = agent or default_agent
        self.secret_store = secret_store

    def snapshot(self, conn: sqlite3.Connection) -> Settings:
        """Fresh settings for each event, so portal changes apply at the next job boundary."""
        return effective_settings(conn, self.base_settings, self.secret_store)

    async def _reply(self, ctx: _Ctx, text: str) -> None:
        await ctx.responder.send_text(ctx.psid, text)
        conv_svc.record_message(ctx.conn, ctx.customer_id, "assistant", text)

    async def process(self, conn: sqlite3.Connection, event: queue.QueuedEvent, now: datetime) -> str:
        """Handle one event. Returns the final queue status ('done' or 'ignored').
        Must be safe to re-run after a crash or a failed send (idempotent)."""
        settings = self.snapshot(conn)
        p = event.payload
        customer = conv_svc.get_or_create_customer(conn, p["page_id"], p["psid"])
        ctx = _Ctx(conn, settings, self._responder_factory(settings), customer["id"], p["psid"], now)

        if event.event_type == "customer_update":
            return await self._handle_customer_update(ctx, p, event.external_event_id)
        cid, raw = ctx.customer_id, p["event"]

        if event.event_type == "echo":
            return self._handle_echo(ctx, raw)

        # Paused: keep recording what customers send, but never reply or act automatically.
        paused = settings.bot_mode != "live"

        if event.event_type == "postback":
            payload = raw["postback"].get("payload", "")
            conv_svc.record_message(conn, cid, "customer", f"[button] {raw['postback'].get('title', payload)}", event.external_event_id)
            return "done" if paused else await self._handle_payload(ctx, payload)

        msg = raw["message"]
        if qr := msg.get("quick_reply"):
            conv_svc.record_message(conn, cid, "customer", msg.get("text") or "[quick reply]", msg["mid"])
            return "done" if paused else await self._handle_payload(ctx, qr.get("payload", ""))

        text = (msg.get("text") or "").strip()
        if not text:
            # Attachments, stickers, etc. are not supported: hand off to a person.
            conv_svc.record_message(conn, cid, "customer", "[attachment]", msg["mid"])
            if not paused and conv_svc.get_conversation(conn, cid)["bot_enabled"]:
                self._hand_off(ctx, "customer sent a photo or attachment", key=f"handoff:{msg['mid']}")
                await self._reply(ctx, "Thanks! I've passed this to our team, and someone will reply here soon.")
            return "done"

        conv_svc.record_message(conn, cid, "customer", text[:4000], msg["mid"])
        if paused or not conv_svc.get_conversation(conn, cid)["bot_enabled"]:
            return "done"  # paused or human handoff: stay silent
        if await self._resend_saved_reply(ctx, msg["mid"]):
            return "done"  # a retry after a failed send: don't run the AI (and change the cart) twice
        result = await self.agent(conn, cid, settings, self.biz, now)
        await self._deliver(ctx, msg["mid"], result)
        return "done"

    async def _deliver(self, ctx: _Ctx, mid: str, result: AgentResult | str | None) -> None:
        """Save the reply under this message's id first, then send it, so a retry resends
        the same reply instead of asking the AI again."""
        if not isinstance(result, AgentResult):
            result = AgentResult(text=result)
        if result.error:
            result = AgentResult(text=AI_DOWN_REPLY, handoff_reason=f"assistant unavailable ({result.error[:100]})")
        if result.handoff_reason:
            self._hand_off(ctx, result.handoff_reason, key=f"handoff:{mid}")
        if result.text:
            conv_svc.record_message(ctx.conn, ctx.customer_id, "assistant", result.text, f"reply:{mid}")
        if result.summary:
            conv_svc.record_message(ctx.conn, ctx.customer_id, "assistant", result.summary.text, f"reply:{mid}:summary")
        if result.text:
            await ctx.responder.send_text(ctx.psid, result.text)
        if result.summary:
            await ctx.responder.send_order_summary(ctx.psid, result.summary.text, result.summary.token)

    async def _resend_saved_reply(self, ctx: _Ctx, mid: str) -> bool:
        rows = ctx.conn.execute(
            "SELECT external_message_id, content FROM messages WHERE external_message_id IN (?, ?) ORDER BY id",
            (f"reply:{mid}", f"reply:{mid}:summary"),
        ).fetchall()
        for r in rows:
            if r["external_message_id"].endswith(":summary"):
                # Tokens aren't stored in history: issue a fresh one for the same cart.
                try:
                    s = order_svc.present_order_summary(ctx.conn, ctx.customer_id, self.biz, ctx.settings, ctx.now)
                except order_svc.OrderError as exc:
                    await ctx.responder.send_text(ctx.psid, str(exc))
                else:
                    await ctx.responder.send_order_summary(ctx.psid, s.text, s.token)
            else:
                await ctx.responder.send_text(ctx.psid, r["content"])
        return bool(rows)

    def _hand_off(self, ctx: _Ctx, reason: str, key: str) -> None:
        conv_svc.set_bot_enabled(ctx.conn, ctx.customer_id, False, reason=reason[:200])
        notify.alert_owners(ctx.conn, ctx.settings, "handoff", notify.handoff_sms(ctx.conn, ctx.customer_id, reason), key)

    async def _handle_customer_update(self, ctx: _Ctx, p: dict, event_key: str) -> str:
        """Order status message triggered by an owner (or expiry). Sent as UPDATE, only inside
        Messenger's 24-hour window."""
        key = f"update:{event_key}"
        if ctx.conn.execute("SELECT 1 FROM messages WHERE external_message_id = ?", (key,)).fetchone():
            return "done"  # already sent (retry after a crash)
        if not notify.within_window(ctx.conn, ctx.customer_id, ctx.now):
            conv_svc.record_message(ctx.conn, ctx.customer_id, "system", f"[not sent: over 24h since last message] {p['text']}")
            return "ignored"
        await ctx.responder.send_update(ctx.psid, p["text"])
        conv_svc.record_message(ctx.conn, ctx.customer_id, "assistant", p["text"], key)
        return "done"

    def _handle_echo(self, ctx: _Ctx, raw: dict) -> str:
        msg = raw["message"]
        if ctx.settings.meta_app_id and str(msg.get("app_id", "")) == ctx.settings.meta_app_id:
            return "ignored"  # our own reply: never respond to ourselves
        # Someone replied by hand from the Page inbox.
        conv_svc.record_message(ctx.conn, ctx.customer_id, "staff", msg.get("text") or "[staff attachment]", msg.get("mid"))
        if ctx.settings.pause_bot_on_staff_reply:
            conv_svc.set_bot_enabled(ctx.conn, ctx.customer_id, False, reason="staff replied manually")
        return "done"

    async def _handle_payload(self, ctx: _Ctx, payload: str) -> str:
        conn, cid = ctx.conn, ctx.customer_id
        if payload.startswith(POSTBACK_CONFIRM):
            token = payload.removeprefix(POSTBACK_CONFIRM)
            try:
                order = order_svc.submit_order(conn, cid, token, self.biz, ctx.settings, ctx.now)
            except (order_svc.ConfirmationInvalid, order_svc.SummaryChanged) as exc:
                await self._resummarize(ctx, str(exc))
                return "done"
            await self._reply(ctx, order_svc.confirmation_text(order, self.biz))
            return "done"
        if payload == POSTBACK_MODIFY:
            conv = conv_svc.get_conversation(conn, cid)
            if conv["state"] != ConversationState.BUILDING_CART:
                conv_svc.set_state(conn, cid, ConversationState.BUILDING_CART)
            await self._reply(ctx, "Sure! What would you like to change?")
            return "done"
        log.info("unknown payload ignored")
        return "ignored"

    async def _resummarize(self, ctx: _Ctx, reason: str) -> None:
        try:
            summary = order_svc.present_order_summary(ctx.conn, ctx.customer_id, self.biz, ctx.settings, ctx.now)
        except order_svc.OrderError as exc:
            await self._reply(ctx, f"{reason} {exc}")
            return
        text = f"{reason}\n\n{summary.text}"
        await ctx.responder.send_order_summary(ctx.psid, text, summary.token)
        conv_svc.record_message(ctx.conn, ctx.customer_id, "assistant", text)


async def run_once(conn: sqlite3.Connection, processor: MessageProcessor, settings: Settings, now: datetime | None = None) -> bool:
    """Claim and process one event. Returns True if an event was handled."""
    now = now or utcnow()
    event = queue.claim_next(conn, now)
    if event is None:
        return False
    try:
        status = await processor.process(conn, event, now)
    except Exception as exc:  # noqa: BLE001 - any failure is retried with backoff
        log.exception("event %s failed (attempt %d)", event.id, event.attempts)
        queue.mark_failed(conn, event, f"{type(exc).__name__}: {exc}", now, settings.worker_max_attempts)
    else:
        queue.mark_done(conn, event.id, status)
    return True


async def _worker_loop(settings: Settings, processor: MessageProcessor) -> None:
    conn = connect(settings.database_path)
    while True:
        if not await run_once(conn, processor, settings):
            await asyncio.sleep(settings.worker_poll_seconds)


def housekeeping_once(conn: sqlite3.Connection, settings: Settings, biz: BusinessInfo, now: datetime) -> None:
    set_marker(conn, "worker.heartbeat", to_db(now))
    released = queue.release_stale(conn, now, settings.worker_lock_timeout_seconds)
    expired = order_svc.expire_pending_orders(conn, now)
    for order_id in expired:
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        notify.queue_customer_update(conn, order, OrderStatus.EXPIRED, biz, now)
        notify.alert_owners(conn, settings, "order_expired",
                            f"Order #{order_id} ({order['customer_name']}) expired without a reply. The customer was told.",
                            f"order:{order_id}:expired", order_id=order_id)
    asked = owner_commands.queue_pickup_checks(conn, settings, biz, now)
    if released or expired or asked:
        log.info("housekeeping released=%d expired_orders=%s pickup_checks=%s", released, expired, asked)


async def _housekeeping(settings: Settings, processor: MessageProcessor) -> None:
    conn = connect(settings.database_path)
    while True:
        try:
            housekeeping_once(conn, processor.snapshot(conn), processor.biz, utcnow())
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("housekeeping failed")
        await asyncio.sleep(10)


async def _notification_loop(settings: Settings, processor: MessageProcessor) -> None:
    """Send queued owner texts through Twilio (picks up portal changes each pass)."""
    conn = connect(settings.database_path)
    while True:
        try:
            await notifications.send_due(conn, processor.snapshot(conn), utcnow())
        except Exception:  # noqa: BLE001
            log.exception("notification sender failed")
        await asyncio.sleep(3)


def build_processor(settings: Settings) -> MessageProcessor:
    return MessageProcessor(
        settings,
        load_business(settings.business_path),
        lambda s: MessengerClient(s),
        secret_store=SecretStore.from_path(settings.master_key_path),
    )


async def run_worker(settings: Settings) -> None:
    processor = build_processor(settings)
    log.info("worker started with concurrency=%d", settings.worker_concurrency)
    await asyncio.gather(
        _housekeeping(settings, processor),
        _notification_loop(settings, processor),
        *(_worker_loop(settings, processor) for _ in range(settings.worker_concurrency)),
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    prepare(settings)
    await run_worker(settings)


if __name__ == "__main__":
    asyncio.run(main())

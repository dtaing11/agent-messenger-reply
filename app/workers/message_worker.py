"""Durable message worker: turns queued Messenger events into state changes and replies.

Run with:  python -m app.workers.message_worker
"""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol

from app.agent.state_machine import ConversationState
from app.config import Settings, get_settings
from app.database.connection import connect, migrate
from app.integrations.messenger import POSTBACK_CONFIRM, POSTBACK_MODIFY, MessengerClient
from app.services import conversation as conv_svc
from app.services import order as order_svc
from app.services.business import BusinessInfo, load_business
from app.services.menu import seed_menu
from app.timeutil import utcnow
from app.workers import queue

log = logging.getLogger(__name__)


class Responder(Protocol):
    async def send_text(self, psid: str, text: str) -> object: ...
    async def send_order_summary(self, psid: str, summary_text: str, token: str) -> object: ...


# The LLM agent (Phase 5) plugs in here: given the customer and their text, return a reply
# (or None to stay silent). Until then a fixed placeholder reply is used.
AgentHandler = Callable[[sqlite3.Connection, int, str], Awaitable[str | None]]


def placeholder_agent(settings: Settings) -> AgentHandler:
    async def handle(conn: sqlite3.Connection, customer_id: int, text: str) -> str | None:
        return settings.placeholder_reply

    return handle


class MessageProcessor:
    def __init__(self, settings: Settings, biz: BusinessInfo, responder: Responder, agent: AgentHandler | None = None):
        self.settings = settings
        self.biz = biz
        self.responder = responder
        self.agent = agent or placeholder_agent(settings)

    async def _reply(self, conn, customer_id: int, psid: str, text: str) -> None:
        await self.responder.send_text(psid, text)
        conv_svc.record_message(conn, customer_id, "assistant", text)

    async def process(self, conn: sqlite3.Connection, event: queue.QueuedEvent, now: datetime) -> str:
        """Handle one event. Returns the final queue status ('done' or 'ignored').
        Must be safe to re-run after a crash or a failed send (idempotent)."""
        p = event.payload
        customer = conv_svc.get_or_create_customer(conn, p["page_id"], p["psid"])
        cid, psid, raw = customer["id"], p["psid"], p["event"]

        if event.event_type == "echo":
            return self._handle_echo(conn, cid, raw)

        if event.event_type == "postback":
            payload = raw["postback"].get("payload", "")
            conv_svc.record_message(conn, cid, "customer", f"[button] {raw['postback'].get('title', payload)}", event.external_event_id)
            return await self._handle_payload(conn, cid, psid, payload, now)

        msg = raw["message"]
        if qr := msg.get("quick_reply"):
            conv_svc.record_message(conn, cid, "customer", msg.get("text") or "[quick reply]", msg["mid"])
            return await self._handle_payload(conn, cid, psid, qr.get("payload", ""), now)

        text = (msg.get("text") or "").strip()
        if not text:
            # Attachments, stickers, etc. are not supported: hand off to a person.
            conv_svc.record_message(conn, cid, "customer", "[attachment]", msg["mid"])
            if conv_svc.get_conversation(conn, cid)["bot_enabled"]:
                conv_svc.set_bot_enabled(conn, cid, False, reason="unsupported attachment")
                await self._reply(conn, cid, psid, "Thanks! I've passed this to our team, and someone will reply here soon.")
            return "done"

        conv_svc.record_message(conn, cid, "customer", text[:4000], msg["mid"])
        if not conv_svc.get_conversation(conn, cid)["bot_enabled"]:
            return "done"  # human handoff: stay silent
        reply = await self.agent(conn, cid, text)
        if reply:
            await self._reply(conn, cid, psid, reply)
        return "done"

    def _handle_echo(self, conn, cid: int, raw: dict) -> str:
        msg = raw["message"]
        if self.settings.meta_app_id and str(msg.get("app_id", "")) == self.settings.meta_app_id:
            return "ignored"  # our own reply: never respond to ourselves
        # Someone replied by hand from the Page inbox.
        conv_svc.record_message(conn, cid, "staff", msg.get("text") or "[staff attachment]", msg.get("mid"))
        if self.settings.pause_bot_on_staff_reply:
            conv_svc.set_bot_enabled(conn, cid, False, reason="staff replied manually")
        return "done"

    async def _handle_payload(self, conn, cid: int, psid: str, payload: str, now: datetime) -> str:
        if payload.startswith(POSTBACK_CONFIRM):
            token = payload.removeprefix(POSTBACK_CONFIRM)
            try:
                order = order_svc.submit_order(conn, cid, token, self.biz, self.settings, now)
            except (order_svc.ConfirmationInvalid, order_svc.SummaryChanged) as exc:
                await self._resummarize(conn, cid, psid, str(exc), now)
                return "done"
            await self._reply(
                conn, cid, psid,
                f"Thank you! Order request #{order['id']} has been sent to our team for acceptance. "
                "I'll message you once they confirm.",
            )
            return "done"
        if payload == POSTBACK_MODIFY:
            conv = conv_svc.get_conversation(conn, cid)
            if conv["state"] != ConversationState.BUILDING_CART:
                conv_svc.set_state(conn, cid, ConversationState.BUILDING_CART)
            await self._reply(conn, cid, psid, "Sure! What would you like to change?")
            return "done"
        log.info("unknown payload ignored")
        return "ignored"

    async def _resummarize(self, conn, cid: int, psid: str, reason: str, now: datetime) -> None:
        try:
            summary = order_svc.present_order_summary(conn, cid, self.biz, self.settings, now)
        except order_svc.OrderError as exc:
            await self._reply(conn, cid, psid, f"{reason} {exc}")
            return
        text = f"{reason}\n\n{summary.text}"
        await self.responder.send_order_summary(psid, text, summary.token)
        conv_svc.record_message(conn, cid, "assistant", text)


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


async def _worker_loop(n: int, settings: Settings, processor: MessageProcessor) -> None:
    conn = connect(settings.database_path)
    while True:
        if not await run_once(conn, processor, settings):
            await asyncio.sleep(settings.worker_poll_seconds)


async def _housekeeping(settings: Settings) -> None:
    conn = connect(settings.database_path)
    while True:
        now = utcnow()
        released = queue.release_stale(conn, now, settings.worker_lock_timeout_seconds)
        expired = order_svc.expire_pending_orders(conn, now)
        if released or expired:
            log.info("housekeeping released=%d expired_orders=%s", released, expired)
        await asyncio.sleep(30)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    conn = connect(settings.database_path)
    migrate(conn)
    seed_menu(conn, settings.menu_path)
    conn.close()
    processor = MessageProcessor(settings, load_business(settings.business_path), MessengerClient(settings))
    log.info("worker started with concurrency=%d", settings.worker_concurrency)
    await asyncio.gather(
        _housekeeping(settings),
        *(_worker_loop(i, settings, processor) for i in range(settings.worker_concurrency)),
    )


if __name__ == "__main__":
    asyncio.run(main())

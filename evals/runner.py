"""Run Sprinkle's eval conversations and score them.

Each case (evals/cases/*.json) is a synthetic customer conversation with a fixed store clock
and expected outcomes. Cases run through the real message worker (quick replies, Confirm
taps, photos, reminders) with fake Messenger and SMS, a throwaway database seeded from
data/menu.json and data/business.json, and the real model through OpenRouter. All checks are
on saved state and sent messages, so no AI judge is needed.

    OPENROUTER_API_KEY=... python -m evals.run                      # every case
    python -m evals.run --category discounts --repeat 3              # one category, 3 runs each
    python -m evals.run --check                                      # validate the dataset only

Never point this at the live Page: it sends nothing to Messenger or Twilio.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.agent.orchestrator import AgentResult, run_agent
from app.config import Settings
from app.database.connection import connect, migrate
from app.integrations.messenger import PICK, POSTBACK_CONFIRM, POSTBACK_MODIFY
from app.services import cart as cart_svc
from app.services.business import BusinessInfo, load_business
from app.services.menu import seed_menu
from app.timeutil import to_db
from app.workers import queue
from app.workers.message_worker import MessageProcessor, housekeeping_once, run_once

ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = ROOT / "evals" / "cases"
PSID, PAGE = "EVALPSID", "EVALPAGE"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 256
# Owner rules that every conversation must keep, whatever the case is about.
ALWAYS_NEVER = {
    "never shows internal button notes": r"\[Buttons shown",
    "never promises the store will call": r"\b(we|the store|our team|someone|staff|they)( will|'ll) (give you a call|call|phone|reach out to|contact) you",
    "never grants a discount": r"(I('ve| have)|we('ve| have)|I'll|I will|we'll) (applied|added|apply|add|give|take|knock)( you)? (a |an |the |your )?"
                               r"(discount|coupon|promo|\d+ ?%|\$?\d+(\.\d\d)? off|free)",
    "never says on the house": r"\bon the house\b",
    "never computes tax": r"(with|including|plus|after) tax[^.]{0,20}\$\d|\$\d+\.\d\d (with|including) tax",
}
LLM = Callable[[Settings, list[dict], list[dict] | None], Awaitable[dict]]


# ---------------------------------------------------------------- dataset

def load_cases(paths: list[Path] | None = None) -> list[dict]:
    cases: list[dict] = []
    for p in sorted(paths or CASES_DIR.glob("*.json")):
        for c in json.loads(p.read_text()):
            c.setdefault("category", p.stem)
            cases.append(c)
    return cases


# ---------------------------------------------------------------- fakes

class FakeMessenger:
    """Records everything Sprinkle sends. Each entry: kind, text, buttons/token."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, psid, text):
        self.sent.append({"kind": "text", "text": text})

    async def send_choices(self, psid, text, options):
        self.sent.append({"kind": "choices", "text": text, "buttons": list(options)})

    async def send_order_summary(self, psid, text, token):
        self.sent.append({"kind": "summary", "text": text, "token": token})

    async def send_update(self, psid, text):
        self.sent.append({"kind": "update", "text": text})

    def last(self, kind: str) -> dict | None:
        return next((m for m in reversed(self.sent) if m["kind"] == kind), None)


@dataclass
class Transcript:
    turns: list[dict] = field(default_factory=list)  # {"customer": ..., "bot": [...]} for the report
    tools: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- running one case

def _settings(tmp: Path, key: str, model: str, fallback: str, semantic: bool) -> Settings:
    return Settings(
        _env_file=None,
        database_path=tmp / "eval.db",
        menu_path=ROOT / "data/menu.json",
        business_path=ROOT / "data/business.json",
        meta_app_secret="eval", meta_verify_token="eval", meta_page_id=PAGE, meta_app_id="EVALAPP",
        owner_phone_numbers=["+15550000001", "+15550000002"],
        bot_mode="live",
        master_key_path=tmp / "keys/master.key",
        uploads_path=tmp / "uploads",
        openrouter_api_key=key, ai_model=model, ai_fallback_model=fallback,
        semantic_search=semantic,
    )


def _local(biz: BusinessInfo, s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(biz.timezone))


class CaseRun:
    def __init__(self, case: dict, settings: Settings, biz: BusinessInfo, llm: LLM | None):
        self.case, self.settings, self.biz = case, settings, biz
        self.conn = connect(settings.database_path)
        migrate(self.conn)
        seed_menu(self.conn, settings.menu_path)
        self.fb = FakeMessenger()
        self.t = Transcript()
        self.now = _local(biz, case["now"])
        self.n = 0

        async def agent(conn, cid, s, b, now):
            r = await run_agent(conn, cid, s, b, now, llm=llm)
            self.t.tools.extend(r.tools_used)
            if isinstance(r, AgentResult) and r.error:
                self.t.errors.append(r.error)
            return r

        async def fetch_photo(url, max_bytes):
            return JPEG

        self.proc = MessageProcessor(settings, biz, self.fb, agent=agent, fetch_photo=fetch_photo)

    def _enqueue(self, msg: dict) -> None:
        self.n += 1
        mid = f"m{self.n}"
        msg = {"mid": mid, **msg}
        event = {"sender": {"id": PSID}, "recipient": {"id": PAGE}, "message": msg}
        queue.enqueue(self.conn, "messenger", f"{PAGE}:{mid}", "message",
                      {"page_id": PAGE, "psid": PSID, "event": event}, customer_key=f"{PAGE}:{PSID}", now=self.now)

    async def _drain(self) -> None:
        for _ in range(20):
            if not await run_once(self.conn, self.proc, self.settings, self.now):
                return

    async def step(self, turn: dict) -> None:
        before = len(self.fb.sent)
        label: str
        if "say" in turn:
            label = turn["say"]
            self._enqueue({"text": turn["say"]})
        elif "tap" in turn:
            last = self.fb.last("choices")
            options = last["buttons"] if last else []
            want = turn["tap"]
            hit = next((o for o in options if o.lower() == want.lower()), None) \
                or next((o for o in options if want.lower() in o.lower()), None)
            label = f"[tap] {hit or want}" + ("" if hit else " (no such button; typed instead)")
            if hit:
                self._enqueue({"text": hit, "quick_reply": {"payload": PICK + hit}})
            else:
                self._enqueue({"text": want})
        elif "confirm" in turn or "modify" in turn:
            s = self.fb.last("summary")
            if s is None:
                label = "[tap Confirm] (no summary was shown)"
                self.t.turns.append({"customer": label, "bot": []})
                return
            if "confirm" in turn:
                label = "[tap] Confirm Order"
                self._enqueue({"text": "Confirm Order", "quick_reply": {"payload": POSTBACK_CONFIRM + s["token"]}})
            else:
                label = "[tap] Modify"
                self._enqueue({"text": "Modify", "quick_reply": {"payload": f"{POSTBACK_MODIFY}:{s['token']}"}})
        elif "photo" in turn:
            label = "[photo]"
            self._enqueue({"attachments": [{"type": "image", "payload": {"url": "https://scontent.xx.fbcdn.net/eval.jpg"}}]})
        elif "wait_minutes" in turn:
            # Time passes in 5-minute ticks, like the worker's housekeeping loop.
            end = self.now + timedelta(minutes=turn["wait_minutes"])
            while self.now < end:
                self.now = min(end, self.now + timedelta(minutes=5))
                housekeeping_once(self.conn, self.settings, self.biz, self.now)
                await self._drain()
            label = f"[{turn['wait_minutes']} minutes pass]"
        else:
            raise ValueError(f"unknown turn {turn}")
        await self._drain()
        if "wait_minutes" not in turn:
            # The app stamps "last customer message" with the wall clock; use the case's clock.
            self.conn.execute("UPDATE conversations SET last_customer_message_at = ?", (to_db(self.now),))
        self.t.turns.append({"customer": label, "bot": [m["text"] for m in self.fb.sent[before:]]})
        self.now += timedelta(minutes=turn.get("then_minutes", 1))

    async def run(self) -> None:
        for turn in self.case["turns"]:
            await self.step(turn)


# ---------------------------------------------------------------- checks

def _cid(conn: sqlite3.Connection) -> int | None:
    r = conn.execute("SELECT id FROM customers WHERE facebook_psid = ?", (PSID,)).fetchone()
    return r["id"] if r else None


def _line_matches(line: cart_svc.CartLine, want: dict) -> bool:
    if line.item_id != want["item_id"]:
        return False
    if "quantity" in want and line.quantity != want["quantity"]:
        return False
    for k, v in (want.get("details") or {}).items():
        got = str((line.details or {}).get(k, ""))
        if v.lower() not in got.lower():
            return False
    names = [n.lower() for _, n in line.modifiers]
    if "add_ons" in want and sorted(names) != sorted(a.lower() for a in want["add_ons"]):
        return False
    return True


def evaluate(run: CaseRun) -> list[tuple[str, bool, str]]:
    """[(check, passed, detail)] for one finished case."""
    exp, conn = run.case.get("expect", {}), run.conn
    out: list[tuple[str, bool, str]] = []
    cid = _cid(conn)
    view = cart_svc.view_cart(conn, cid, 0) if cid else None
    lines = view.lines if view else []
    bot = [m["text"] or "" for m in run.fb.sent]
    order = conn.execute("SELECT * FROM orders WHERE customer_id = ? ORDER BY id DESC", (cid,)).fetchone() if cid else None
    order_lines = []
    if order:
        order_lines = conn.execute("SELECT * FROM order_items WHERE order_id = ?", (order["id"],)).fetchall()

    def add(name, ok, detail=""):
        out.append((name, bool(ok), detail))

    if "cart" in exp:
        want = exp["cart"]
        # After a confirmed order the cart is gone: check the order's items instead.
        if order is not None and not lines:
            got = [f"{r['quantity']} x {r['item_id']}" for r in order_lines]
            for w in want:
                hit = any(r["item_id"] == w["item_id"] and r["quantity"] == w.get("quantity", r["quantity"]) for r in order_lines)
                add(f"order has {w.get('quantity', '')} {w['item_id']}", hit, f"order: {got}")
        else:
            got = [f"{l.quantity} x {l.item_id} {l.details or ''} {[n for _, n in l.modifiers] or ''}".strip() for l in lines]
            for w in want:
                add(f"cart has {w.get('quantity', '')} {w['item_id']} {w.get('details', '') or ''} {w.get('add_ons', '')}".strip(),
                    any(_line_matches(l, w) for l in lines), f"cart: {got}")
            if exp.get("cart_exact", True):
                add("cart has nothing extra", len(lines) == len(want), f"cart: {got}")
    if exp.get("cart_empty"):
        add("cart is empty", not lines, f"cart: {[l.item_id for l in lines]}")
    if "not_in_cart" in exp:
        for item in exp["not_in_cart"]:
            add(f"no {item} in cart", all(l.item_id != item for l in lines))
    if "subtotal_cents" in exp:
        got = order["subtotal_cents"] if order is not None and not lines else (view.totals.subtotal_cents if view else 0)
        add(f"subtotal {exp['subtotal_cents']}", got == exp["subtotal_cents"], f"got {got}")
    if "pickup" in exp:
        got_dt = view.pickup_time if view and view.pickup_time else None
        if got_dt is None and order is not None:
            got_dt = datetime.fromisoformat(order["pickup_time"].replace("Z", "+00:00"))
        got = got_dt.astimezone(ZoneInfo(run.biz.timezone)).strftime("%Y-%m-%d %H:%M") if got_dt else None
        add(f"pickup {exp['pickup']}", got == exp["pickup"], f"got {got}")
    if "pickup_name" in exp:
        got = (view.customer_name if view else None) or (order["customer_name"] if order is not None else None)
        add(f"pickup name {exp['pickup_name']}", (got or "").lower() == exp["pickup_name"].lower(), f"got {got}")
    if "summary_shown" in exp:
        add("summary shown" if exp["summary_shown"] else "no summary shown",
            (run.fb.last("summary") is not None) == exp["summary_shown"])
    if "order_submitted" in exp:
        add("order submitted" if exp["order_submitted"] else "no order submitted", (order is not None) == exp["order_submitted"])
    if "handoff" in exp:
        enabled = conn.execute("SELECT bot_enabled FROM conversations WHERE customer_id = ?", (cid,)).fetchone() if cid else None
        handed = enabled is not None and enabled[0] == 0
        add("handed to staff" if exp["handoff"] else "not handed to staff", handed == exp["handoff"])
    if "photos_attached" in exp:
        n = conn.execute("SELECT COUNT(*) FROM customer_photos WHERE customer_id = ? AND (cart_id IS NOT NULL OR order_id IS NOT NULL)",
                         (cid,)).fetchone()[0] if cid else 0
        add(f"{exp['photos_attached']} photo(s) kept with the order", n == exp["photos_attached"], f"got {n}")
    for pat in exp.get("reply_any", []):
        add(f"some reply matches /{pat}/", any(re.search(pat, b, re.I) for b in bot))
    for pat in exp.get("last_reply_any", []):
        last = next((b for b in reversed(bot) if b), "")
        add(f"last reply matches /{pat}/", re.search(pat, last, re.I), last[:160])
    for pat in exp.get("never", []):
        bad = next((b for b in bot if re.search(pat, b, re.I)), None)
        add(f"no reply matches /{pat}/", bad is None, (bad or "")[:160])
    for name in exp.get("tools_called", []):
        add(f"called {name}", name in run.t.tools)
    for name in exp.get("tools_not_called", []):
        add(f"did not call {name}", name not in run.t.tools)
    for w in exp.get("cart_line_never", []):
        bad = next((l for l in lines if _line_matches(l, w)), None)
        add(f"no cart line like {w}", bad is None, f"found {bad.quantity} x {bad.item_id} {bad.details}" if bad else "")
    for rule in exp.get("max_replies_matching", []):
        n = sum(1 for b in bot if re.search(rule["pattern"], b, re.I))
        add(f"at most {rule['max']} replies match /{rule['pattern']}/", n <= rule["max"], f"got {n}")
    if "order_status" in exp:
        got = order["status"] if order is not None else None
        add(f"order status {exp['order_status']}", got == exp["order_status"], f"got {got}")
    if exp.get("buttons_any"):
        buttons = [b for m in run.fb.sent for b in m.get("buttons", [])]
        add(f"buttons include one of {exp['buttons_any']}",
            any(w.lower() in b.lower() for w in exp["buttons_any"] for b in buttons), f"buttons: {buttons}")
    for name, pat in ALWAYS_NEVER.items():
        bad = next((b for b in bot if re.search(pat, b, re.I)), None)
        add(name, bad is None, (bad or "")[:160])
    add("no AI errors", not run.t.errors, "; ".join(run.t.errors)[:200])
    return out


async def run_case(case: dict, key: str, model: str, fallback: str, semantic: bool, llm: LLM | None = None) -> dict:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        settings = _settings(tmp, key, model, fallback, semantic)
        biz = load_business(settings.business_path)
        r = CaseRun(case, settings, biz, llm)
        try:
            await r.run()
            checks = evaluate(r)
        except Exception as exc:  # noqa: BLE001 - one broken case shouldn't stop the run
            checks = [("case ran", False, f"{type(exc).__name__}: {exc}"[:300])]
        finally:
            r.conn.close()
    return {"id": case["id"], "category": case["category"], "passed": all(ok for _, ok, _ in checks),
            "checks": [{"check": c, "passed": ok, "detail": d} for c, ok, d in checks],
            "transcript": r.t.turns, "tools": r.t.tools}

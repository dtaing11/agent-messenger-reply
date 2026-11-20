"""Tools the LLM may call. Every tool runs deterministic backend code; the customer is
always the one from the event (never an LLM argument). The LLM cannot submit an order:
that only happens when the customer taps Confirm on the summary (handled in the worker)."""

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import Settings
from app.integrations import messenger
from app.services import cart as cart_svc
from app.services import (
    custom_items,
    customers,
    discounts,
    menu,
    notify,
    phone,
    search,
    sessions,
)
from app.services import order as order_svc
from app.services.business import BusinessInfo, PickupValidation, validate_pickup_time
from app.services.pricing import money
from app.timeutil import from_db, local, to_db

log = logging.getLogger(__name__)

TOOLS: list[dict] = [
    {"type": "function", "function": {
        "name": "search_menu", "description": "Find menu items by name, nickname or what the customer describes (\"something chocolatey\", \"the long one with cream\"). Returns item ids, descriptions, prices (before tax) and add-on modifiers.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "maxLength": 100}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "browse_menu", "description": "A short overview for menu questions: with no category, the menu's categories and price ranges; with a category (e.g. \"Bubble Tea\"), its items, each with its own price, and same_price. Use search_menu for an item's flavors and add-ons.",
        "parameters": {"type": "object", "properties": {"category": {"type": "string", "maxLength": 60}}}}},
    {"type": "function", "function": {
        "name": "offer_choices", "description": "Show tap-to-answer buttons under your reply when you ask the customer to pick from a short list (kinds, flavors, yes/no). Up to 13 options, each 20 characters or fewer (Messenger cuts longer ones). Write the question in your reply as usual. A tap comes back as the customer's message with that exact text.",
        "parameters": {"type": "object", "properties": {
            "options": {"type": "array", "items": {"type": "string", "maxLength": 20}, "minItems": 2, "maxItems": 13}},
            "required": ["options"]}}},
    {"type": "function", "function": {
        "name": "search_knowledge", "description": "Look up approved store information: hours, holidays, location, phone, payment, policies, special orders.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string", "maxLength": 200}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_business_info", "description": "Store name, address, phone, hours and pickup rules.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_cart", "description": "The customer's current cart: lines (with line_id), subtotal, pickup time, name, and what is still missing.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "add_to_cart", "description": "Add an item. If search_menu shows required_choices (like flavor), put the customer's pick in details, e.g. details.flavor = \"Chocolate\"; for different flavors add separate lines. For the Special Design Donut, include details.message and details.design (and details.name if the message has a name).",
        "parameters": {"type": "object", "properties": {
            "item_id": {"type": "string", "maxLength": 80},
            "quantity": {"type": "integer", "minimum": 1, "maximum": 100},
            "modifier_ids": {"type": "array", "items": {"type": "string", "maxLength": 100}, "maxItems": 8},
            "details": {"type": "object", "properties": {
                "message": {"type": "string", "maxLength": 60}, "name": {"type": "string", "maxLength": 40},
                "design": {"type": "string", "maxLength": 300}},
                "additionalProperties": {"type": "string", "maxLength": 60}}},
            "required": ["item_id", "quantity"]}}},
    {"type": "function", "function": {
        "name": "update_cart_item", "description": "Change a cart line's quantity (0 removes it), add-ons or details. Use line_id from get_cart.",
        "parameters": {"type": "object", "properties": {
            "line_id": {"type": "integer"}, "quantity": {"type": "integer", "minimum": 0, "maximum": 100},
            "modifier_ids": {"type": "array", "items": {"type": "string", "maxLength": 100}, "maxItems": 8},
            "details": {"type": "object", "properties": {
                "message": {"type": "string", "maxLength": 60}, "name": {"type": "string", "maxLength": 40},
                "design": {"type": "string", "maxLength": 300}},
                "additionalProperties": {"type": "string", "maxLength": 60}}},
            "required": ["line_id", "quantity"]}}},
    {"type": "function", "function": {
        "name": "remove_from_cart", "description": "Remove a cart line by line_id.",
        "parameters": {"type": "object", "properties": {"line_id": {"type": "integer"}}, "required": ["line_id"]}}},
    {"type": "function", "function": {
        "name": "check_pickup_time", "description": "Check if a pickup time works. pickup_time is store-local 'YYYY-MM-DD HH:MM' (24-hour).",
        "parameters": {"type": "object", "properties": {"pickup_time": {"type": "string", "maxLength": 16}}, "required": ["pickup_time"]}}},
    {"type": "function", "function": {
        "name": "set_pickup_details", "description": "Save the pickup time (store-local 'YYYY-MM-DD HH:MM'), pickup name, phone number and/or notes. Any field may be omitted.",
        "parameters": {"type": "object", "properties": {
            "pickup_time": {"type": "string", "maxLength": 16}, "customer_name": {"type": "string", "maxLength": 80},
            "customer_phone": {"type": "string", "maxLength": 30, "description": "US phone number as the customer typed it"},
            "notes": {"type": "string", "maxLength": 300}}}}},
    {"type": "function", "function": {
        "name": "show_order_summary", "description": "Send the customer the exact order summary with Confirm and Modify buttons. Call only when items, pickup time, name and phone number are set.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_my_orders", "description": "This customer's recent orders and their status.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "cancel_my_order", "description": "Cancel one of this customer's orders if it's still allowed. Only when the customer clearly asks to cancel.",
        "parameters": {"type": "object", "properties": {"order_id": {"type": "integer"}}, "required": ["order_id"]}}},
    {"type": "function", "function": {
        "name": "resume_unfinished_order", "description": "For an earlier unfinished order that was set aside: restore it as the current order, or discard it. Use only after the customer answers.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["restore", "discard"]},
            "replace_current": {"type": "boolean", "description": "Replace items already in the new order (only if the customer said so)."}},
            "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "request_human_support", "description": "Hand the conversation to the store team and alert them. The bot then stays quiet for this customer.",
        "parameters": {"type": "object", "properties": {"reason": {"type": "string", "maxLength": 200}}, "required": ["reason"]}}},
]


TOPPING_CATEGORY = "bubble_tea"


def _topping_offer(conn, item_id: str) -> str | None:
    """Owner: no drink comes with a topping, but offer them once, leading with tapioca pearls."""
    item = menu.get_item(conn, item_id)
    if item is None or item["category"] != TOPPING_CATEGORY:
        return None
    mods = menu.get_modifiers(conn, item_id)
    lead = next((m for m in mods if "tapioca" in m["name"].lower()), mods[0] if mods else None)
    if lead is None:
        return None
    return (f"No toppings were added. Offer toppings once for this order, leading with {lead['name']} "
            f"(+{money(lead['price_delta_cents'])}, modifier_id {lead['id']}), which goes with almost every drink. "
            "Add one only if the customer says yes; if they say no, don't offer again.")


# The owners' rule: the bot never gives discounts, so nothing it writes for the team or the
# customer may suggest one (see services.discounts).
NO_DISCOUNT_TEXT = ("That text mentions a discount or a free item. Sprinkle never gives discounts, so it can't go "
                    "on the order. Prices are as listed on the menu.")

# How each order status reads to the customer. A missed pickup is an owner-only record:
# the assistant only ever sees "closed", the same as a picked-up order.
CUSTOMER_STATUS = {
    "PENDING_ACCEPTANCE": "waiting for the team to accept", "ACCEPTED": "accepted", "PREPARING": "being prepared",
    "READY": "ready for pickup", "REJECTED": "not accepted", "CANCELLED": "cancelled",
    "EXPIRED": "not accepted in time", "COMPLETED": "closed", "NO_SHOW": "closed",
}


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    customer_id: int
    settings: Settings
    biz: BusinessInfo
    now: datetime
    summary: order_svc.OrderSummary | None = None  # set by show_order_summary
    handoff_reason: str | None = None  # set by request_human_support
    quick_replies: tuple[str, ...] = ()  # set by offer_choices
    log: list[str] = field(default_factory=list)


def _whole(value, what: str = "quantity", low: int = 0, high: int = 10**9) -> int:
    """A whole number from tool arguments: 1.9 or "lots" is refused instead of silently becoming 1."""
    if isinstance(value, bool):
        raise ValueError(f"The {what} must be a whole number.")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"The {what} must be a whole number. Ask the customer how many.")
        value = int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        value = int(value.strip())
    if not isinstance(value, int) or not (low <= value <= high):
        raise ValueError(f"The {what} must be a whole number from {low} to {high}.")
    return value


def _parse_local(ctx: ToolContext, value: str) -> datetime:
    try:
        naive = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError("Use the format YYYY-MM-DD HH:MM (store local time, 24-hour).")
    return naive.replace(tzinfo=ZoneInfo(ctx.biz.timezone))


def _fmt_local(dt: datetime, tz: str) -> str:
    d = local(dt, tz)
    return f"{d:%a %b} {d.day} at {d:%-I:%M %p}"


def _validate_for_cart(ctx: ToolContext, when: datetime, view: cart_svc.CartView | None = None) -> PickupValidation:
    """Pickup check that includes the notice the cart's items need (Special Design Donut: day before)."""
    view = view or cart_svc.view_cart(ctx.conn, ctx.customer_id, ctx.settings.tax_rate_bps)
    days, notice = view.advance_notice(ctx.biz.pickup.notice_cutoff_hour)
    return validate_pickup_time(ctx.biz, when, ctx.now, days, notice, ordered_at=view.notice_ordered_at(ctx.now))


def _suggestions(ctx: ToolContext, pv: PickupValidation) -> list[str]:
    return [_fmt_local(s, ctx.biz.timezone) + f" ({s:%Y-%m-%d %H:%M})" for s in pv.suggestions]


def _cart_json(ctx: ToolContext, view: cart_svc.CartView | None = None) -> dict:
    view = view or cart_svc.view_cart(ctx.conn, ctx.customer_id, ctx.settings.tax_rate_bps)
    missing = [m.replace(" ", "_") for m in order_svc.missing_details(view, ctx.settings.require_customer_phone)]
    out = {
        "lines": [{
            "line_id": l.line_id, "item_id": l.item_id, "name": l.name, "quantity": l.quantity,
            "add_ons": [n for _, n in l.modifiers], "details": l.details or None,
            "unit_price": money(l.unit_price_cents), "line_total": money(l.line_total_cents),
            "surcharge_to_confirm": l.surcharge_pending or None, "available": l.available,
        } for l in view.lines],
        "subtotal_before_tax": money(view.totals.subtotal_cents),
        "pickup_time": _fmt_local(view.pickup_time, ctx.biz.timezone) if view.pickup_time else None,
        "pickup_name": view.customer_name,
        "pickup_phone": phone.display(view.customer_phone) or None,
        "notes": view.notes or None,
        "missing": missing,
        "unavailable_items": view.unavailable or None,
    }
    if view.pickup_time is not None and not view.is_empty:
        pv = _validate_for_cart(ctx, view.pickup_time, view)
        if not pv.ok:  # e.g. a design donut was added to a same-day order, or the time has passed
            out["pickup_time_problem"] = {"reason": pv.reason, "suggestions": _suggestions(ctx, pv)}
    if (not view.customer_name or not view.customer_phone) and (saved := customers.saved_contact(ctx.conn, ctx.customer_id)):
        out["saved_contact_from_last_order"] = saved  # offer to reuse; never assume
    return out


def _item_json(conn: sqlite3.Connection, it: sqlite3.Row, cutoff_hour: int = 3) -> dict:
    category = it["category_name"] if "category_name" in it.keys() else it["category"].replace("_", " ")
    out = {"item_id": it["id"], "name": it["name"], "category": category,
           "unit": it["unit_label"], "price_before_tax": money(it["price_cents"]), "max_quantity": it["max_quantity"]}
    if (desc := (it["description"] or "").strip()):
        out["description"] = desc
    mods = menu.get_modifiers(conn, it["id"])
    if mods:
        out["add_ons"] = [{"modifier_id": m["id"], "name": m["name"], "price": money(m["price_delta_cents"])} for m in mods]
    choices = custom_items.parse_choices(it["choices"])
    if fixed := [c for c in choices if not c.is_pool and not c.mix]:
        out["required_choices"] = {c.key: list(c.options) for c in fixed}
        if rules := [r for c in fixed if (r := c.rule_text())]:
            out["choice_rules"] = rules
    for c in (c for c in choices if c.mix):
        out["pick_a_mix"] = {
            "detail_key": c.key, "how_many": c.count, "choose_from": list(c.options),
            "how_to_add": f"Ask how they'd like the {c.count} mixed, then put it in details.{c.key}, e.g. "
                          "'6 Chocolate Icing with sprinkles, 6 Glazed', or 'store's choice' if they let us pick. "
                          "Sprinkles only go on icing flavors, never on Glazed or Cinnamon Sugar.",
        }
    for c in (c for c in choices if c.is_pool):
        out["pick_a_mix"] = {
            "detail_key": c.key, "how_many": c.count,
            "choose_from": menu.pool_labels(conn, c.pool_category, c.pool_price_cents),
            "how_to_add": f"Ask which {c.count} they want (any mix), then put it in details.{c.key}, "
                          "e.g. '2 Twist (Chocolate), 2 Cinnamon Roll, 2 Pinecone'.",
        }
    rules = custom_items.CustomRules.from_json(it["custom_rules"])
    if rules:
        out["requires_details"] = {"message": rules.questions.get("message") or custom_items.DEFAULT_QUESTIONS["message"],
                                   "design": rules.questions.get("design") or custom_items.DEFAULT_QUESTIONS["design"]}
        if rules.min_days_ahead:
            out["order_ahead"] = custom_items.notice_text(it["name"], rules.min_days_ahead, cutoff_hour)
        if rules.name_free_letters is not None:
            out["long_name_rule"] = f"Names longer than {rules.name_free_letters} letters have an extra charge the store confirms."
    if it["description"]:
        out["description"] = it["description"]
    return out


async def execute(ctx: ToolContext, name: str, raw_args: str | dict) -> dict:
    try:
        args = json.loads(raw_args or "{}") if isinstance(raw_args, str) else (raw_args or {})
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        ctx.log.append(name)
        return _dispatch(ctx, name, args)
    except (cart_svc.CartError, order_svc.OrderError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, cart_svc.DetailsNeeded):
            return {"error": "details_needed", "ask_customer": exc.questions}
        return {"error": str(exc)[:300]}
    except Exception as exc:  # noqa: BLE001 - a tool bug must never crash the whole reply (a retry would redo tools)
        log.exception("tool %s failed", name)
        return {"error": f"That didn't work ({type(exc).__name__}). Try something else or ask the customer."}


def _dispatch(ctx: ToolContext, name: str, a: dict) -> dict:
    conn, cid = ctx.conn, ctx.customer_id
    if name == "search_menu":
        rows, exact = search.find_menu_items(conn, str(a["query"])[:100], limit=8,
                                             min_score=ctx.settings.semantic_min_score)
        if not rows:
            return {"items": [], "note": "No matching items on the menu."}
        out = {"items": [_item_json(conn, r, ctx.biz.pickup.notice_cutoff_hour) for r in rows]}
        if not exact:
            out["note"] = ("No item matched those words exactly; these are the closest by meaning. Offer the ones that fit, "
                           "or ask what they mean. Don't say we have something these items don't show.")
        return out
    if name == "browse_menu":
        return menu.browse(conn, str(a.get("category") or "")[:60] or None)
    if name == "offer_choices":
        opts = []
        for o in a.get("options") or []:
            o = " ".join(str(o).replace("|", "/").split())
            if o and o.lower() not in (x.lower() for x in opts):
                opts.append(o)
        if len(opts) < 2:
            raise ValueError("Give at least 2 different options.")
        if long := [o for o in opts if len(o) > messenger.QUICK_REPLY_TITLE_MAX]:
            raise ValueError(f"Each option must be {messenger.QUICK_REPLY_TITLE_MAX} characters or fewer, or Messenger cuts it off. "
                             f"Too long: {', '.join(repr(o) for o in long)}. Shorten them and call offer_choices again.")
        if discounts.any_discount(opts):
            raise ValueError("Buttons can't offer discounts, deals or free items. Sprinkle never gives discounts.")
        ctx.quick_replies = tuple(opts[:13])
        return {"ok": True, "note": "Buttons will appear under your reply. Still ask the question in your reply."}
    if name == "search_knowledge":
        docs = search.find_knowledge(conn, str(a["query"])[:200], min_score=ctx.settings.semantic_min_score)
        return {"documents": docs} if docs else {"documents": [], "note": "No approved information found. Don't guess; offer to connect them with the team."}
    if name == "get_business_info":
        b = ctx.biz
        return {"name": b.name, "store": b.store_name, "landmark": b.landmark, "address": b.address,
                "map_link": b.maps_url or None,
                "pickup_note": "Messenger orders are picked up only at this store. Other Town Donut stores can't fill them.",
                "phone": b.raw.get("phone"), "payment": b.raw.get("payment"), "price_note": b.raw.get("price_note"),
                "hours": {d: (f"{h[0]:%-I:%M %p}-{h[1]:%-I:%M %p}" if h else "closed") for d, h in b.hours.items()},
                "last_pickup_time": b.last_pickup_text(),
                "pickup_rules": {"min_notice_minutes": b.pickup.min_lead_minutes, "max_days_ahead": b.pickup.max_days_ahead,
                                 "last_pickup": f"{b.pickup.last_pickup_before_close_minutes} minutes before closing"}}
    if name == "get_cart":
        return _cart_json(ctx)
    if name in ("add_to_cart", "update_cart_item") and discounts.any_discount(a.get("details") or {}):
        return {"error": NO_DISCOUNT_TEXT}
    if name == "add_to_cart":
        view = cart_svc.add_item(conn, cid, str(a["item_id"]), _whole(a["quantity"], low=1, high=100), a.get("modifier_ids") or None,
                                 a.get("details") or None, now=ctx.now)
        out = {"ok": True, "cart": _cart_json(ctx, view)}
        if not a.get("modifier_ids") and (tip := _topping_offer(conn, str(a["item_id"]))):
            # Once per order: only the first bubble tea brings the reminder.
            if conn.execute("UPDATE carts SET topping_offered_at = ? WHERE id = ? AND topping_offered_at IS NULL",
                            (to_db(ctx.now), view.cart_id)).rowcount:
                out["next_step"] = tip
        return out
    if name == "update_cart_item":
        view = cart_svc.update_item(conn, cid, _whole(a["line_id"], "line_id"), _whole(a["quantity"], high=100), a.get("modifier_ids"), a.get("details"))
        return {"ok": True, "cart": _cart_json(ctx, view)}
    if name == "remove_from_cart":
        return {"ok": True, "cart": _cart_json(ctx, cart_svc.remove_item(conn, cid, _whole(a["line_id"], "line_id")))}
    if name == "check_pickup_time":
        pv = _validate_for_cart(ctx, _parse_local(ctx, str(a["pickup_time"])))
        return {"ok": pv.ok, "pickup_time": _fmt_local(pv.pickup_local, ctx.biz.timezone), "reason": pv.reason,
                "suggestions": _suggestions(ctx, pv) or None}
    if name == "set_pickup_details":
        if discounts.any_discount([a.get("notes"), a.get("customer_name")]):
            return {"ok": False, "reason": NO_DISCOUNT_TEXT}
        when = None
        if a.get("pickup_time"):
            when = _parse_local(ctx, str(a["pickup_time"]))
            pv = _validate_for_cart(ctx, when)
            if not pv.ok:
                return {"ok": False, "reason": pv.reason, "suggestions": _suggestions(ctx, pv)}
        view = cart_svc.set_pickup_details(conn, cid, pickup_time=when, customer_name=a.get("customer_name") or None,
                                           customer_phone=a.get("customer_phone") or None,
                                           notes=a.get("notes") if a.get("notes") is not None else None)
        return {"ok": True, "cart": _cart_json(ctx, view)}
    if name == "show_order_summary":
        ctx.summary = order_svc.present_order_summary(conn, cid, ctx.biz, ctx.settings, ctx.now)
        return {"ok": True, "note": "Summary with Confirm/Modify buttons will be sent to the customer. Do not repeat it."}
    if name == "get_my_orders":
        rows = conn.execute(
            "SELECT id, status, pickup_time, subtotal_cents FROM orders WHERE customer_id = ? ORDER BY id DESC LIMIT 5", (cid,)
        ).fetchall()
        return {"orders": [{"order_id": r["id"], "status": CUSTOMER_STATUS.get(r["status"], r["status"].replace("_", " ").lower()),
                            "pickup_time": _fmt_local(from_db(r["pickup_time"]), ctx.biz.timezone),
                            "subtotal_before_tax": money(r["subtotal_cents"])} for r in rows]}
    if name == "cancel_my_order":
        o = order_svc.cancel_by_customer(conn, cid, _whole(a["order_id"], "order_id"), ctx.settings)
        notify.alert_owners(ctx.conn, ctx.settings, "order_cancelled",
                            f"Order #{o['id']} ({o['customer_name']}) was CANCELLED by the customer on Messenger.",
                            f"order:{o['id']}:cancelled", order_id=o["id"])
        return {"ok": True, "order_id": o["id"], "status": "cancelled"}
    if name == "resume_unfinished_order":
        if a.get("action") == "discard":
            sessions.discard_set_aside(conn, cid)
            return {"ok": True, "note": "Earlier order dropped. Start fresh."}
        view = sessions.restore_set_aside(conn, cid, ctx.now, bool(a.get("replace_current")))
        out = {"ok": True, "cart": _cart_json(ctx, view)}
        if view.pickup_time is None:
            out["note"] = "The old pickup time had passed, so ask for a new one."
        return out
    if name == "request_human_support":
        reason = str(a.get("reason", "customer needs help"))[:200]
        ctx.handoff_reason = reason
        return {"ok": True, "note": "The team has been alerted. Tell the customer someone will reply here soon."}
    raise ValueError(f"unknown tool {name}")

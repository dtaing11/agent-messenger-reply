"""Checks that every case still fits the menu: item ids, add-ons and choice values exist, and
expected subtotals match today's prices. Runs in the normal test suite (no model calls), so a
menu change that breaks a case shows up right away."""

from __future__ import annotations

import json
from datetime import datetime

from evals.runner import ROOT

TURN_KEYS = {"say", "tap", "confirm", "modify", "photo", "wait_minutes"}
EXPECT_KEYS = {"cart", "cart_exact", "cart_empty", "not_in_cart", "subtotal_cents", "pickup", "pickup_name",
               "summary_shown", "order_submitted", "handoff", "photos_attached", "reply_any", "last_reply_any",
               "never", "cart_line_never", "max_replies_matching", "order_status", "tools_called", "tools_not_called", "buttons_any"}
TOOLS = {"search_menu", "browse_menu", "offer_choices", "search_knowledge", "get_business_info", "get_cart",
         "add_to_cart", "update_cart_item", "remove_from_cart", "check_pickup_time", "set_pickup_details",
         "show_order_summary", "get_my_orders", "cancel_my_order", "resume_unfinished_order", "request_human_support"}


def check_dataset(cases: list[dict]) -> list[str]:
    menu = {it["id"]: it for it in json.loads((ROOT / "data/menu.json").read_text())["items"]}
    problems: list[str] = []
    seen: set[str] = set()
    for c in cases:
        cid = c.get("id", "?")
        if cid in seen:
            problems.append(f"{cid}: duplicate id")
        seen.add(cid)
        for k in ("id", "description", "now", "turns", "expect"):
            if k not in c:
                problems.append(f"{cid}: missing {k}")
        try:
            datetime.strptime(c.get("now", ""), "%Y-%m-%d %H:%M")
        except ValueError:
            problems.append(f"{cid}: now must be 'YYYY-MM-DD HH:MM' (store time)")
        for t in c.get("turns", []):
            if not (set(t) & TURN_KEYS):
                problems.append(f"{cid}: unknown turn {t}")
        exp = c.get("expect", {})
        for k in set(exp) - EXPECT_KEYS:
            problems.append(f"{cid}: unknown expectation {k}")
        for t in exp.get("tools_called", []) + exp.get("tools_not_called", []):
            if t not in TOOLS:
                problems.append(f"{cid}: unknown tool {t}")
        for item in exp.get("not_in_cart", []):
            if item not in menu:
                problems.append(f"{cid}: unknown item {item}")
        total, priced = 0, True
        for line in exp.get("cart", []):
            it = menu.get(line["item_id"])
            if it is None:
                problems.append(f"{cid}: unknown item {line['item_id']}")
                priced = False
                continue
            unit = it["price_cents"]
            mods = {m["name"].lower(): m["price_delta_cents"] for m in it.get("modifiers", [])}
            for a in line.get("add_ons", []):
                if a.lower() not in mods:
                    problems.append(f"{cid}: {line['item_id']} has no add-on {a!r}")
                unit += mods.get(a.lower(), 0)
            choices = {ch["key"]: ch for ch in it.get("choices", [])}
            for k, v in (line.get("details") or {}).items():
                ch = choices.get(k)
                if ch is None and k not in ("message", "design", "name"):
                    problems.append(f"{cid}: {line['item_id']} has no choice {k!r}")
                elif ch and ch.get("options") and not ch.get("mix") and not any(v.lower() in o.lower() for o in ch["options"]):
                    problems.append(f"{cid}: {line['item_id']}.{k} has no option like {v!r}")
            if "quantity" not in line:
                priced = False
            total += unit * line.get("quantity", 1)
        if "subtotal_cents" in exp and priced and exp.get("cart") and exp["subtotal_cents"] != total:
            problems.append(f"{cid}: expected subtotal {exp['subtotal_cents']} but menu prices give {total}")
    return problems

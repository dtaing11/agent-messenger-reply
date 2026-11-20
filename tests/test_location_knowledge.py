"""Pickup location on every summary/confirmation, message splitting, and the store FAQ."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.integrations.messenger import split_message
from app.services import cart, knowledge
from app.services import order as order_svc
from app.services.business import load_business
from tests.conftest import NOW, ROOT

PICKUP = datetime(2026, 11, 19, 7, 30, tzinfo=ZoneInfo("America/Chicago"))


def test_real_business_location():
    biz = load_business(ROOT / "data/business.json")
    assert biz.store_name == "Town Donut Watson"
    assert biz.address == "35505 LA-16 #3C, Denham Springs, LA 70706"
    assert biz.landmark == "Oak Point Plaza, next to Subway"
    assert biz.maps_url.startswith("https://www.google.com/maps/")


def test_summary_and_confirmation_show_location(conn, customer_id, biz, settings):
    cart.add_item(conn, customer_id, "glazed_dozen", 1)
    cart.set_pickup_details(conn, customer_id, pickup_time=PICKUP, customer_name="Jessica", customer_phone="225-555-0123")
    s = order_svc.present_order_summary(conn, customer_id, biz, settings, NOW)
    assert "Pickup location: Town Donut Watson, Oak Point Plaza, next to Subway" in s.text
    assert "35505 LA-16 #3C" in s.text and "Map: https://www.google.com/maps/" in s.text
    assert "make sure this is the store" in s.text
    assert s.text.rstrip().endswith("Would you like to submit this order?")
    order = order_svc.submit_order(conn, customer_id, s.token, biz, settings, NOW)
    text = order_svc.confirmation_text(order, biz)
    assert f"#{order['id']}" in text and "Oak Point Plaza" in text and "35505 LA-16 #3C" in text


def test_split_message_keeps_everything():
    lines = [f"{i} x Item number {i} — $1.00" for i in range(150)] + ["Pickup location: Town Donut Watson"]
    text = "\n".join(lines)
    parts = split_message(text)
    assert len(parts) > 1 and all(len(p) <= 2000 for p in parts)
    assert "\n".join(parts) == text
    assert parts[-1].endswith("Town Donut Watson")
    assert split_message("short") == ["short"]
    long_line = "word " * 900
    assert all(len(p) <= 2000 for p in split_message(long_line))


def test_knowledge_seed_and_search(conn):
    path = ROOT / "data/knowledge/faq.json"
    assert knowledge.seed_knowledge(conn, path, "watson") >= 8
    top = knowledge.search(conn, "where are you located?")
    assert top and top[0]["id"] == "location" and "Oak Point Plaza" in top[0]["answer"]
    assert knowledge.search(conn, "are you open on christmas")[0]["id"] in ("holidays", "hours")
    assert knowledge.search(conn, "birthday donut")[0]["id"] == "special_design_donut"
    assert knowledge.search(conn, "the and you") == []


def test_knowledge_reseed_versions_changed_docs(conn, tmp_path: Path):
    p = tmp_path / "faq.json"
    p.write_text('{"documents": [{"id": "a", "question": "Q?", "answer": "one"}]}')
    knowledge.seed_knowledge(conn, p, "watson")
    knowledge.seed_knowledge(conn, p, "watson")  # unchanged: no new version
    p.write_text('{"documents": [{"id": "a", "question": "Q?", "answer": "two"}]}')
    knowledge.seed_knowledge(conn, p, "watson")
    rows = conn.execute("SELECT version, approval_status, answer FROM knowledge_documents WHERE id='a' ORDER BY version").fetchall()
    assert [tuple(r) for r in rows] == [(1, "retired", "one"), (2, "approved", "two")]
    assert [d["answer"] for d in knowledge.search(conn, "Q")] in ([], ["two"])

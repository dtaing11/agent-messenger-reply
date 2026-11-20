from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.agent.state_machine import InvalidTransition, OrderStatus
from app.services import cart
from app.services import order as order_svc
from tests.conftest import NOW

PICKUP = datetime(2026, 11, 19, 7, 30, tzinfo=ZoneInfo("America/Chicago"))


@pytest.fixture
def ready_cart(conn, customer_id):
    cart.add_item(conn, customer_id, "glazed_dozen", 2)
    cart.add_item(conn, customer_id, "sausage_kolache", 6)
    cart.set_pickup_details(conn, customer_id, pickup_time=PICKUP, customer_name="Jessica")
    return customer_id


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_summary_requires_details(conn, customer_id, biz, settings):
    cart.add_item(conn, customer_id, "glazed_dozen", 1)
    with pytest.raises(order_svc.OrderError, match="pickup time, pickup name"):
        order_svc.present_order_summary(conn, customer_id, biz, settings, NOW)


def test_summary_text_matches_plan_example(conn, ready_cart, biz, settings):
    s = order_svc.present_order_summary(conn, ready_cart, biz, settings, NOW)
    assert "2 x Dozen Glazed Donuts — $24.00" in s.text
    assert "6 x Sausage Kolache — $15.00" in s.text
    assert "Subtotal: $39.00 before tax" in s.text
    assert "Pickup: Thu Nov 19 at 7:30 AM" in s.text
    assert order_svc.TAX_REMINDER in s.text
    assert s.total_cents == 3900


def test_summary_with_tax(conn, ready_cart, biz, settings):
    settings.tax_rate_bps = 825
    s = order_svc.present_order_summary(conn, ready_cart, biz, settings, NOW)
    assert "Tax: $3.22" in s.text and "Total: $42.22" in s.text
    assert order_svc.TAX_REMINDER not in s.text


def test_submit_creates_order_items_event_and_one_sms_per_owner(conn, ready_cart, biz, settings):
    s = order_svc.present_order_summary(conn, ready_cart, biz, settings, NOW)
    o = order_svc.submit_order(conn, ready_cart, s.token, biz, settings, NOW)
    assert o["status"] == "PENDING_ACCEPTANCE" and o["subtotal_cents"] == 3900
    assert count(conn, "order_items") == 2
    assert count(conn, "order_events") == 1
    recipients = [r[0] for r in conn.execute("SELECT recipient FROM notifications ORDER BY id")]
    assert recipients == settings.owner_phone_numbers
    body = conn.execute("SELECT body FROM notifications LIMIT 1").fetchone()[0]
    assert f"NEW ORDER #{o['id']}" in body and f"Reply YES {o['id']}" in body
    assert "Pickup: Nov 19, 7:30 AM CST" in body
    assert "Subtotal: $39.00 (before tax)" in body
    # Cart is closed and a new one starts empty.
    assert cart.view_cart(conn, ready_cart).is_empty


def test_duplicate_confirmation_is_idempotent(conn, ready_cart, biz, settings):
    s = order_svc.present_order_summary(conn, ready_cart, biz, settings, NOW)
    a = order_svc.submit_order(conn, ready_cart, s.token, biz, settings, NOW)
    b = order_svc.submit_order(conn, ready_cart, s.token, biz, settings, NOW)
    assert a["id"] == b["id"]
    assert count(conn, "orders") == 1 and count(conn, "notifications") == 2


def test_cart_change_invalidates_token(conn, ready_cart, biz, settings):
    s = order_svc.present_order_summary(conn, ready_cart, biz, settings, NOW)
    cart.add_item(conn, ready_cart, "coffee_medium", 1)
    with pytest.raises(order_svc.ConfirmationInvalid):
        order_svc.submit_order(conn, ready_cart, s.token, biz, settings, NOW)
    assert count(conn, "orders") == 0


def test_price_change_requires_new_confirmation(conn, ready_cart, biz, settings):
    s = order_svc.present_order_summary(conn, ready_cart, biz, settings, NOW)
    conn.execute("UPDATE menu_items SET price_cents = 1300 WHERE id = 'glazed_dozen'")
    with pytest.raises(order_svc.SummaryChanged):
        order_svc.submit_order(conn, ready_cart, s.token, biz, settings, NOW)
    s2 = order_svc.present_order_summary(conn, ready_cart, biz, settings, NOW)
    assert s2.total_cents == 4100
    assert order_svc.submit_order(conn, ready_cart, s2.token, biz, settings, NOW)["total_cents"] == 4100


def test_token_expires(conn, ready_cart, biz, settings):
    s = order_svc.present_order_summary(conn, ready_cart, biz, settings, NOW)
    later = NOW + timedelta(minutes=settings.confirmation_token_ttl_minutes + 1)
    with pytest.raises(order_svc.ConfirmationInvalid, match="expired"):
        order_svc.submit_order(conn, ready_cart, s.token, biz, settings, later)


def test_token_bound_to_customer(conn, ready_cart, biz, settings):
    from app.services.conversation import get_or_create_customer

    s = order_svc.present_order_summary(conn, ready_cart, biz, settings, NOW)
    other = get_or_create_customer(conn, "PAGE1", "INTRUDER")["id"]
    with pytest.raises(order_svc.ConfirmationInvalid):
        order_svc.submit_order(conn, other, s.token, biz, settings, NOW)


def test_pickup_revalidated_at_submit(conn, ready_cart, biz, settings):
    # Pickup 45 min from now is valid at summary time but not 20 min later (30 min lead).
    soon = datetime(2026, 11, 18, 8, 45, tzinfo=ZoneInfo("America/Chicago"))
    cart.set_pickup_details(conn, ready_cart, pickup_time=soon)
    s = order_svc.present_order_summary(conn, ready_cart, biz, settings, NOW)
    with pytest.raises(order_svc.SummaryChanged, match="notice"):
        order_svc.submit_order(conn, ready_cart, s.token, biz, settings, NOW + timedelta(minutes=20))


def test_large_order_flagged(conn, customer_id, biz, settings):
    cart.add_item(conn, customer_id, "assorted_dozen", 10)  # $140
    cart.set_pickup_details(conn, customer_id, pickup_time=PICKUP, customer_name="School")
    s = order_svc.present_order_summary(conn, customer_id, biz, settings, NOW)
    o = order_svc.submit_order(conn, customer_id, s.token, biz, settings, NOW)
    assert o["needs_review"] == 1
    assert "REVIEW:" in conn.execute("SELECT body FROM notifications LIMIT 1").fetchone()[0]


@pytest.fixture
def pending_order(conn, ready_cart, biz, settings):
    s = order_svc.present_order_summary(conn, ready_cart, biz, settings, NOW)
    return order_svc.submit_order(conn, ready_cart, s.token, biz, settings, NOW)


def test_owner_flow(conn, pending_order):
    oid = pending_order["id"]
    for target in (OrderStatus.ACCEPTED, OrderStatus.READY, OrderStatus.COMPLETED):
        assert order_svc.transition_order(conn, oid, target, "owner:+15550000001")["status"] == target
    events = [r[0] for r in conn.execute("SELECT to_status FROM order_events WHERE order_id = ? ORDER BY id", (oid,))]
    assert events == ["PENDING_ACCEPTANCE", "ACCEPTED", "READY", "COMPLETED"]


def test_second_owner_cannot_override(conn, pending_order):
    oid = pending_order["id"]
    order_svc.transition_order(conn, oid, OrderStatus.ACCEPTED, "owner:+15550000001")
    with pytest.raises(InvalidTransition):
        order_svc.transition_order(conn, oid, OrderStatus.REJECTED, "owner:+15550000002")


def test_customer_cancel_rules(conn, pending_order, settings, ready_cart):
    oid = pending_order["id"]
    order_svc.transition_order(conn, oid, OrderStatus.ACCEPTED, "owner")
    with pytest.raises(order_svc.OrderError):
        order_svc.cancel_by_customer(conn, ready_cart, oid, settings)
    settings.customer_can_cancel_accepted = True
    assert order_svc.cancel_by_customer(conn, ready_cart, oid, settings)["status"] == "CANCELLED"


def test_pending_orders_expire(conn, pending_order, settings):
    assert order_svc.expire_pending_orders(conn, NOW + timedelta(minutes=5)) == []
    later = NOW + timedelta(minutes=settings.pending_order_expiry_minutes + 1)
    assert order_svc.expire_pending_orders(conn, later) == [pending_order["id"]]

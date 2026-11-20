from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.services import cart
from app.services import order as order_svc
from app.services.cart import CartError, DetailsNeeded
from tests.conftest import NOW

PICKUP = datetime(2026, 11, 19, 7, 30, tzinfo=ZoneInfo("America/Chicago"))
ITEM = "special_design_donut"


def test_requires_message_and_design(conn, customer_id):
    with pytest.raises(DetailsNeeded) as exc:
        cart.add_item(conn, customer_id, ITEM, 1)
    assert exc.value.missing == ["message", "design"]
    assert "What kind of design" in str(exc.value)
    with pytest.raises(DetailsNeeded) as exc:
        cart.add_item(conn, customer_id, ITEM, 1, details={"message": "Happy Birthday"})
    assert exc.value.missing == ["design"]


def test_short_name_no_surcharge(conn, customer_id):
    view = cart.add_item(conn, customer_id, ITEM, 1,
                         details={"message": "Happy Bday Emma", "name": "Emma", "design": "pink, sprinkles"})
    line = view.lines[0]
    assert line.unit_price_cents == 2289 and not line.surcharge_pending and line.needs_review
    assert line.details == {"message": "Happy Bday Emma", "name": "Emma", "design": "pink, sprinkles"}


def test_seven_letter_name_is_free(conn, customer_id):
    view = cart.add_item(conn, customer_id, ITEM, 1, details={"message": "Happy Bday Jessica", "name": "Jessica", "design": "blue"})
    assert not view.lines[0].surcharge_pending


def test_long_name_marks_surcharge_pending(conn, customer_id):
    view = cart.add_item(conn, customer_id, ITEM, 1,
                         details={"message": "Happy Bday Elizabeth", "name": "Elizabeth", "design": "purple"})
    assert view.lines[0].surcharge_pending and view.totals.subtotal_cents == 2289


def test_configured_surcharge_is_added(conn, customer_id):
    conn.execute(
        "UPDATE menu_items SET custom_rules = json_set(custom_rules, '$.long_name_surcharge_cents', 300) WHERE id = ?", (ITEM,)
    )
    view = cart.add_item(conn, customer_id, ITEM, 2, details={"message": "Happy Bday Alexander", "name": "Alexander", "design": "cars"})
    assert view.lines[0].unit_price_cents == 2589 and view.totals.subtotal_cents == 5178


def test_different_messages_are_separate_lines(conn, customer_id):
    cart.add_item(conn, customer_id, ITEM, 1, details={"message": "Merry Christmas", "design": "red and green"})
    view = cart.add_item(conn, customer_id, ITEM, 1, details={"message": "Happy Birthday", "design": "balloons"})
    assert len(view.lines) == 2


def test_message_too_long(conn, customer_id):
    with pytest.raises(CartError):
        cart.add_item(conn, customer_id, ITEM, 1, details={"message": "x" * 41, "design": "any"})


def test_details_rejected_on_regular_item(conn, customer_id):
    with pytest.raises(CartError):
        cart.add_item(conn, customer_id, "coffee_medium", 1, details={"message": "hi", "design": "x"})


def test_summary_and_sms_show_design_and_surcharge(conn, customer_id, biz, settings):
    cart.add_item(conn, customer_id, ITEM, 1,
                  details={"message": "Happy Bday Elizabeth", "name": "Elizabeth", "design": "purple unicorn"})
    cart.set_pickup_details(conn, customer_id, pickup_time=PICKUP, customer_name="Mom")
    s = order_svc.present_order_summary(conn, customer_id, biz, settings, NOW)
    assert 'Message: "Happy Bday Elizabeth"' in s.text and "Design: purple unicorn" in s.text
    assert order_svc.LONG_NAME_NOTE in s.text and order_svc.TAX_REMINDER in s.text
    o = order_svc.submit_order(conn, customer_id, s.token, biz, settings, NOW)
    assert o["needs_review"] == 1
    sms = conn.execute("SELECT body FROM notifications LIMIT 1").fetchone()[0]
    assert "Design: purple unicorn" in sms and "LONG NAME" in sms and "REVIEW:" in sms

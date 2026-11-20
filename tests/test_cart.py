import pytest

from app.services import cart
from app.services.cart import CartError


def test_add_merges_same_item_and_bumps_version(conn, customer_id):
    v0 = cart.view_cart(conn, customer_id).version
    cart.add_item(conn, customer_id, "glazed_dozen", 1)
    view = cart.add_item(conn, customer_id, "glazed_dozen", 1)
    assert len(view.lines) == 1 and view.lines[0].quantity == 2
    assert view.version == v0 + 2
    assert view.totals.subtotal_cents == 2400


def test_modifiers_price_and_are_separate_lines(conn, customer_id):
    cart.add_item(conn, customer_id, "sausage_kolache", 6)
    view = cart.add_item(conn, customer_id, "sausage_kolache", 2, ["sausage_kolache_cheese"])
    assert len(view.lines) == 2
    assert view.lines[1].unit_price_cents == 300
    assert view.totals.subtotal_cents == 6 * 250 + 2 * 300


def test_modifier_order_does_not_matter(conn, customer_id):
    cart.add_item(conn, customer_id, "sausage_kolache", 1, ["sausage_kolache_jalapeno", "sausage_kolache_cheese"])
    view = cart.add_item(conn, customer_id, "sausage_kolache", 1, ["sausage_kolache_cheese", "sausage_kolache_jalapeno"])
    assert len(view.lines) == 1 and view.lines[0].quantity == 2


@pytest.mark.parametrize(
    "item,qty,mods",
    [("nope", 1, None), ("glazed_dozen", 0, None), ("glazed_dozen", 999, None), ("glazed_dozen", 1, ["sausage_kolache_cheese"])],
)
def test_rejects_invalid(conn, customer_id, item, qty, mods):
    with pytest.raises(CartError):
        cart.add_item(conn, customer_id, item, qty, mods)


def test_merge_respects_max_quantity(conn, customer_id):
    cart.add_item(conn, customer_id, "glazed_dozen", 15)
    with pytest.raises(CartError):
        cart.add_item(conn, customer_id, "glazed_dozen", 10)


def test_update_and_remove(conn, customer_id):
    view = cart.add_item(conn, customer_id, "coffee_medium", 2)
    line = view.lines[0].line_id
    assert cart.update_item(conn, customer_id, line, 5).lines[0].quantity == 5
    assert cart.remove_item(conn, customer_id, line).is_empty


def test_cannot_touch_other_customers_line(conn, customer_id):
    from app.services.conversation import get_or_create_customer

    other = get_or_create_customer(conn, "PAGE1", "PSID2")["id"]
    line = cart.add_item(conn, other, "coffee_medium", 1).lines[0].line_id
    with pytest.raises(CartError):
        cart.update_item(conn, customer_id, line, 3)


def test_unavailable_item_flagged(conn, customer_id):
    cart.add_item(conn, customer_id, "coffee_medium", 1)
    conn.execute("UPDATE menu_items SET available = 0 WHERE id = 'coffee_medium'")
    assert cart.view_cart(conn, customer_id).unavailable == ["Medium Coffee"]


def test_pickup_name_validation(conn, customer_id):
    with pytest.raises(CartError):
        cart.set_pickup_details(conn, customer_id, customer_name="   ")

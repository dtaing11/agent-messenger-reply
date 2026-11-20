import pytest

from app.services.pricing import compute_totals, money, tax_cents


def test_tax_rounds_half_up():
    assert tax_cents(3900, 825) == 322  # 321.75 -> 322
    assert tax_cents(200, 825) == 17  # 16.5 -> 17
    assert tax_cents(100, 0) == 0


def test_tax_rejects_negative():
    with pytest.raises(ValueError):
        tax_cents(-1, 825)


def test_totals():
    t = compute_totals([2400, 1500], 825)
    assert (t.subtotal_cents, t.tax_cents, t.total_cents, t.tax_included) == (3900, 322, 4222, True)
    assert compute_totals([], 0).total_cents == 0


def test_money():
    assert money(3900) == "$39.00"
    assert money(123456) == "$1,234.56"
    assert money(5) == "$0.05"

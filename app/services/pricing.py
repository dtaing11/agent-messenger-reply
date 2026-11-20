"""Deterministic totals in integer cents."""

from dataclasses import dataclass


def tax_cents(subtotal_cents: int, tax_rate_bps: int) -> int:
    """Tax rounded half-up to the cent. 825 bps = 8.25%."""
    if subtotal_cents < 0 or tax_rate_bps < 0:
        raise ValueError("negative amounts are not allowed")
    return (subtotal_cents * tax_rate_bps + 5000) // 10000


@dataclass(frozen=True)
class Totals:
    subtotal_cents: int
    tax_cents: int
    total_cents: int
    tax_included: bool  # False when tax is disabled (summaries say "before tax")


def compute_totals(line_totals: list[int], tax_rate_bps: int) -> Totals:
    subtotal = sum(line_totals)
    tax = tax_cents(subtotal, tax_rate_bps)
    return Totals(subtotal, tax, subtotal + tax, tax_rate_bps > 0)


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"

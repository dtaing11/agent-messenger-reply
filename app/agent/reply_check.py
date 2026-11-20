"""A light check on Sprinkle's own words before they reach the customer.

The backend already makes sure the model can't set a price, give a discount or submit an
order. This catches the words: a reply that promises a discount (the owners' rule is that
the bot never gives one), or says an order is confirmed when the team hasn't accepted any.
Such a reply gets one rewrite; if it still breaks the rule, a safe fixed reply goes out
instead. Dollar amounts that match nothing known are only logged.
"""

import logging
import re
import sqlite3

from app.services import discounts

log = logging.getLogger(__name__)

CLAIM = re.compile(
    r"\border\b[^.!?\n]{0,40}?\b(?:is|has been|was|been|are|'s)\s+(?:now\s+)?"
    r"(?:confirmed|accepted|placed|approved|booked)\b"
    r"|\b(?:i|we)(?:'ve| have)?\s+(?:confirmed|accepted|placed|booked|approved)\s+(?:your|the)\s+order\b",
    re.I,
)
_CONDITIONAL = re.compile(r"\b(?:once|when|after|until|till|if|as soon as|before)\b[^.!?\n]*$", re.I)
_NEGATED = re.compile(r"\b(?:not|never|yet to)\b|n't\b", re.I)
MONEY = re.compile(r"\$\s*(\d{1,4}(?:,\d{3})*(?:\.\d{2})?)")
ACCEPTED_LIKE = ("ACCEPTED", "PREPARING", "READY", "COMPLETED")

REWRITE = {
    "discount": ("Sprinkle never gives discounts, coupons, free items or lower prices. Rewrite your reply without "
                 "offering or mentioning one; prices are as listed on the menu."),
    "confirmed": ("The team hasn't accepted any order for this customer, so never say an order is confirmed, accepted "
                  "or placed. If they confirmed a summary, say it was sent to the team and they'll get a message here "
                  "once it's accepted. Rewrite your reply."),
}
FALLBACK = {
    "discount": "Sorry, we don't offer discounts or coupons through Messenger. Prices are as listed on our menu.",
    "confirmed": "Our team reviews every order and will message you here once it's accepted.",
}


def _claims_confirmed(text: str) -> bool:
    for m in CLAIM.finditer(text):
        before = text[max(0, m.start() - 40):m.start()]
        if _CONDITIONAL.search(before) or _NEGATED.search(m.group(0)):
            continue  # "once your order is accepted", "your order hasn't been accepted yet"
        return True
    return False


def problems(conn: sqlite3.Connection, customer_id: int, text: str) -> list[str]:
    out = []
    if discounts.mentions_discount(text):
        out.append("discount")
    if _claims_confirmed(text):
        marks = ",".join("?" * len(ACCEPTED_LIKE))
        accepted = conn.execute(f"SELECT 1 FROM orders WHERE customer_id = ? AND status IN ({marks}) LIMIT 1",
                                (customer_id, *ACCEPTED_LIKE)).fetchone()
        if accepted is None:
            out.append("confirmed")
    return out


def rewrite_note(issues: list[str]) -> str:
    return " ".join(REWRITE[i] for i in issues)


def fallback(issues: list[str]) -> str:
    return " ".join(FALLBACK[i] for i in issues) + " Is there anything else I can help with?"


def _cents(amount: str) -> int:
    whole, _, frac = amount.replace(",", "").partition(".")
    return int(whole) * 100 + int((frac or "0").ljust(2, "0")[:2])


def unknown_amounts(conn: sqlite3.Connection, text: str, context: list[dict]) -> list[str]:
    """Dollar amounts in the reply that appear nowhere the model could have read them (tool
    results, the cart note, the chat) and aren't a menu price, a price plus one topping, or a
    small multiple of those. Logged, never blocked: a customer's own tax estimate is fine."""
    found = MONEY.findall(text)
    if not found:
        return []
    seen = " ".join(str(m.get("content") or "") for m in context)
    allowed = {_cents(a) for a in MONEY.findall(seen)}
    prices = [r[0] for r in conn.execute("SELECT price_cents FROM menu_items WHERE archived = 0")]
    deltas = [r[0] for r in conn.execute("SELECT DISTINCT price_delta_cents FROM menu_modifiers WHERE price_delta_cents > 0")]
    for p in prices:
        for unit in (p, *(p + d for d in deltas)):
            allowed.update(unit * q for q in range(1, 25))
    allowed.update(d * q for d in deltas for q in range(1, 7))
    odd = [f"${a}" for a in found if _cents(a) not in allowed]
    if odd:
        log.warning("reply quoted amounts that match nothing known: %s", ", ".join(odd))
    return odd

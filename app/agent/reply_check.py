"""A light check on Sprinkle's own words before they reach the customer.

The backend already makes sure the model can't set a price, give a discount or submit an
order. This catches the words: a reply that promises a discount (the owners' rule is that
the bot never gives one), or says an order is confirmed when the team hasn't accepted any.
It also catches calling the Iced Coffee "Community Coffee" (it's the house blend; only Coffee
and Cappuccino are Community Coffee), promising that the store will call, text or reach out
(it never does about orders: updates come here in Messenger), and a total lower than what the
cart really costs. Such a reply gets one rewrite; if it still breaks the rule, a safe fixed
reply goes out instead. Other dollar amounts that match nothing known are only logged.
"""

import logging
import re
import sqlite3

from app.services import cart as cart_svc
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
_ICED = re.compile(r"\bice[d]?\s+coffees?\b", re.I)
_COMMUNITY = re.compile(r"\bcommunity\s+coffee\b", re.I)
_BLEND_CLAUSES = re.compile(r"[.!?;\n]+|\bbut\b|\bwhile\b|\bwhereas\b|\bhowever\b", re.I)
MONEY = re.compile(r"\$\s*(\d{1,4}(?:,\d{3})*(?:\.\d{2})?)")
# "We'll call you when it's ready", "The store will reach out", "Someone will text you".
_CONTACT = re.compile(
    r"\b(?:we|i|they|someone|somebody|staff|the\s+(?:store|team|staff|owners?|shop|bakery)|our\s+(?:team|staff|owners?))\b"
    r"[^.!?\n]{0,25}?(?:\bwill|'ll|\bare\s+going\s+to|\bis\s+going\s+to|\bgonna|\bmay|\bmight|\bcan|\bshould)\s+"
    r"(?:\w+\s+)?(?:call|text|phone|ring|reach\s+out|contact|get\s+(?:in\s+touch|back\s+to\s+you\s+by\s+(?:phone|text))|"
    r"give\s+you\s+a\s+(?:call|ring|text))\b",
    re.I,
)
_CLAUSE = re.compile(r"[!?;\n]+|\.(?!\d)")
# "Your total is $5.00" when the cart costs more: a lower price in disguise.
_TOTAL = re.compile(r"\b(?:total|subtotal|comes\s+to|come\s+to|all\s+for|everything\s+for|whole\s+(?:order|thing)\s+for)\b"
                    r"[^$\n]{0,20}?\$\s*(\d{1,4}(?:,\d{3})*(?:\.\d{2})?)", re.I)
_TOTAL_OTHER = re.compile(r"\b(?:would|without|if|minus|remove\w*|last|previous|earlier|before|other|#\d)", re.I)
ACCEPTED_LIKE = ("ACCEPTED", "PREPARING", "READY", "COMPLETED")

REWRITE = {
    "discount": ("Sprinkle never gives discounts, coupons, free items or lower prices. Rewrite your reply without "
                 "offering or mentioning one; prices are as listed on the menu."),
    "confirmed": ("The team hasn't accepted any order for this customer, so never say an order is confirmed, accepted "
                  "or placed. If they confirmed a summary, say it was sent to the team and they'll get a message here "
                  "once it's accepted. Rewrite your reply."),
    "blend": ("Our Iced Coffee is made with our house blend, not Community Coffee. Only Coffee and Cappuccino are "
              "Community Coffee. Rewrite your reply."),
    "contact": ("The store never calls, texts or reaches out to customers about orders; every update comes here in "
                "Messenger. Rewrite your reply without promising a call, text or other contact."),
    "total": ("That total is lower than what the cart costs. Sprinkle never lowers prices: quote totals only from "
              "the cart or the order summary. Rewrite your reply."),
}
FALLBACK = {
    "discount": "Sorry, we don't offer discounts or coupons through Messenger. Prices are as listed on our menu.",
    "confirmed": "Our team reviews every order and will message you here once it's accepted.",
    "blend": "Just so you know, our Iced Coffee is made with our house blend. Coffee and Cappuccino are made with Community Coffee.",
    "contact": "You'll get every update about your order right here in Messenger.",
    "total": "Prices are as listed on our menu, and your order summary will show the exact total.",
}


def _claims_confirmed(text: str) -> bool:
    for m in CLAIM.finditer(text):
        before = text[max(0, m.start() - 40):m.start()]
        if _CONDITIONAL.search(before) or _NEGATED.search(m.group(0)):
            continue  # "once your order is accepted", "your order hasn't been accepted yet"
        return True
    return False


def _iced_called_community(text: str) -> bool:
    """"Our iced coffee is Community Coffee" yes; "the iced coffee isn't Community Coffee" or
    "Coffee is Community Coffee while iced coffee is our house blend" no."""
    for clause in _BLEND_CLAUSES.split(text or ""):
        if (_ICED.search(clause) and _COMMUNITY.search(clause) and not _NEGATED.search(clause)
                and "house blend" not in clause.lower()):
            return True
    return False


_CONTACT_NOT_US = re.compile(r"^\s+(?:it|that|this|them\s+a|us|the\s+(?:store|shop|bakery|team)|ahead)\b", re.I)


def _promises_contact(text: str) -> bool:
    for m in _CONTACT.finditer(text or ""):
        if _NEGATED.search(m.group(0)) or _CONTACT_NOT_US.match(text[m.end():]):
            continue  # "we won't call you"; "I can call it a dozen"; "they can call the store"
        return True
    return False


def _total_below_cart(conn: sqlite3.Connection, customer_id: int, text: str) -> bool:
    row = conn.execute("SELECT id FROM carts WHERE customer_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
                       (customer_id,)).fetchone()
    if row is None:
        return False
    subtotal = cart_svc.view_cart_by_id(conn, row["id"]).totals.subtotal_cents
    if not subtotal:
        return False
    for clause in _CLAUSE.split(text or ""):
        if _TOTAL_OTHER.search(clause):
            continue  # "it would be $5 without the coffee", "your last order was $9"
        if any(_cents(a) < subtotal for a in _TOTAL.findall(clause)):
            return True
    return False


def problems(conn: sqlite3.Connection, customer_id: int, text: str) -> list[str]:
    out = []
    if discounts.mentions_discount(text):
        out.append("discount")
    elif _total_below_cart(conn, customer_id, text):
        out.append("total")
    if _iced_called_community(text):
        out.append("blend")
    if _promises_contact(text):
        out.append("contact")
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

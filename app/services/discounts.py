"""The owners' rule: the bot never gives discounts. This spots discount talk in any text the
bot writes or passes on: its replies, its tap buttons, and free-text fields that reach the
owners (design-donut message and design, pickup name, order notes).

It looks one clause at a time, and a clause that says "not", "can't", "aren't" and so on
before the discount words is an honest refusal ("Sorry, I can't apply a discount"), not an offer.
"""

import re

_OFFER = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:%|percent)(?:\s*(?:off|discount))?"
    r"|\b(?:\d+|a|an|one|two|three|four|five|ten|fifty)\s+(?:dollars?|bucks?|cents?|quarters?)\s+off\b"
    r"|\$\s*\d+(?:\.\d{1,2})?\s*off\b"
    r"|\b(?:knock|take|shave|cut|chop|drop|lop)(?:s|ped|ping|ing)?\b[^.!?]{0,30}?\boff\b"
    r"|\bdiscount\w*|\bcoupons?\b|\bvouchers?\b|\bpromo(?:tion)?s?\b(?:\s+codes?)?"
    r"|\bprice[\s-]?match\w*"
    r"|\bbogo\b|\bbuy\s+(?:one|1|two|2)\b[^.!?]{0,20}\bget\b"
    r"|\b(?:employee|staff|regulars?'?|friends?(?:\s+and\s+family)?|family|military|senior|student|vip|loyalty|member)\s+"
    r"(?:price|pricing|rate|deal|discount)s?\b"
    r"|\bhalf[\s-]?(?:price|off)\b|\bhalf\s+the\s+price\b"
    r"|\bcomplimentary\b|\bon\s+the\s+house\b|\bfree\s+of\s+charge\b|\bno\s+charge\b|\bat\s+no\s+(?:extra\s+)?cost\b"
    r"|\bon\s+us\b|\bthrow(?:s|ing)?\s+in\b|\bdeals?\b"
    r"|\b(?:lower|reduce|drop|cut)\w*\b[^.!?]{0,20}\bprices?\b|\bcheaper\b|\bmark(?:ed)?[\s-]?down\b",
    re.I,
)
_FREE = re.compile(r"\bfree\b", re.I)
# "gluten-free", "sugar free", "feel free", "free to pick": not a giveaway.
_FREE_OK_BEFORE = re.compile(r"(?:-|\b(?:gluten|sugar|nut|dairy|fat|caffeine|allergen|peanut|egg|hands|feel|toll)\s*)$", re.I)
_FREE_OK_AFTER = re.compile(r"^\s*(?:to|-)\b", re.I)
_NEGATION = re.compile(r"\b(?:not|no|never|cannot|can't|cant|don't|dont|doesn't|won't|isn't|aren't|unable|without|"
                       r"nor)\b|n't\b", re.I)
_CLAUSES = re.compile(r"[.!?;,:\n]+|\bbut\b|\bhowever\b|\bthough\b", re.I)


def _clauses(text: str) -> list[str]:
    return [c for c in _CLAUSES.split(text or "") if c.strip()]


def _offers_in(clause: str) -> bool:
    hits = [m.start() for m in _OFFER.finditer(clause)]
    for m in _FREE.finditer(clause):
        if not _FREE_OK_BEFORE.search(clause[:m.start()]) and not _FREE_OK_AFTER.match(clause[m.end():]):
            hits.append(m.start())
    return any(not _NEGATION.search(clause[:start]) for start in hits)


def mentions_discount(text: str) -> bool:
    """True when the text offers or claims a discount, free item or lower price."""
    return any(_offers_in(c) for c in _clauses(text))


def any_discount(values) -> str | None:
    """The first string among `values` (nested dicts/lists too) that mentions a discount."""
    if isinstance(values, dict):
        values = list(values.values())
    if isinstance(values, (list, tuple)):
        for v in values:
            if (hit := any_discount(v)) is not None:
                return hit
        return None
    return values if isinstance(values, str) and mentions_discount(values) else None

"""The owners' rule: the bot never gives discounts. This spots discount talk in any text the
bot writes or passes on: its replies, its tap buttons, and free-text fields that reach the
owners (design-donut message and design, pickup name, order notes).

It looks one clause at a time. A "not", "can't", "aren't" and so on only turns a match into
an honest refusal ("Sorry, I can't apply a discount") when it sits a few words before it, and
not when an offer ("I'll", "you get") comes in between: "No problem, I'll knock a dollar off"
is still an offer. Phrases like "no problem" or "don't worry" aren't refusals at all. A clause
that only continues a list ("we don't do coupons, discounts, or deals") follows the clause
before it.

Ordinary order talk is left alone: taking an item off the order, a topping thrown in for its
price, "Deal!" as a yes, or a dozen being cheaper per donut.
"""

import re

_MONEY_WORDS = r"(?:\$\s*\d|\d+(?:\.\d+)?\s*(?:%|percent|cents?|dollars?|bucks?)|\b(?:a|an|one|two|three|four|five|ten|fifty)\s+(?:dollars?|bucks?|cents?|quarters?)\b|\b(?:a\s+(?:bit|little)|something|some\s+money)\b)"
_PRICE_NOUN = r"(?:the|your|that|this)\s+(?:price|total|bill|cost|charge)"

_OFFER = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:%|percent)(?:\s*(?:off|discount))?"
    r"|\b(?:\d+|a|an|one|two|three|four|five|ten|fifty)\s+(?:dollars?|bucks?|cents?|quarters?)\s+off\b"
    r"|\$\s*\d+(?:\.\d{1,2})?\s*off\b"
    # "knock/shave/lop ... off" is price talk; "take/cut/drop ... off" only with an amount or the price
    r"|\b(?:knock|shave|lop)(?:s|ed|ped|ping|ing)?\b[^.!?]{0,30}?\boff\b"
    rf"|\b(?:take|took|taking|takes|cut|cuts|cutting|chop\w*|drop\w*)\b(?=[^.!?]{{0,30}}?{_MONEY_WORDS}[^.!?]{{0,10}}?\boff\b)"
    rf"|\b(?:take|took|taking|takes|cut|cuts|cutting|drop\w*|knock\w*)\s+(?:it\s+|that\s+)?off\s+(?:{_MONEY_WORDS}|{_PRICE_NOUN})"
    rf"|\boff\s+{_PRICE_NOUN}\b"
    r"|\bdiscount\w*|\bcoupons?\b|\bvouchers?\b|\bpromo(?:tion)?s?\b(?:\s+codes?)?"
    r"|\bprice[\s-]?match\w*"
    r"|\bbogo\b|\bbuy\s+(?:one|1|two|2)\b[^.!?]{0,20}\bget\b"
    r"|\b(?:\d+|two|three|four)[\s-]+for[\s-]+(?:1|one)\b"
    r"|\b(?:employee|staff|regulars?'?|friends?(?:\s+and\s+family)?|family|military|senior|student|vip|loyalty|member)\s+"
    r"(?:price|pricing|rate|deal|discount)s?\b"
    r"|\bhalf[\s-]?(?:price|off)\b|\bhalf\s+the\s+price\b"
    r"|\bcomplimentary\b|\bon\s+the\s+house\b|\bfree\s+of\s+charge\b|\bno\s+charge\b|\bat\s+no\s+(?:extra\s+)?cost\b"
    r"|\bon\s+us\b|(?<!count\s)(?<!rely\s)(?<!depend\s)\bon\s+me\b"
    r"|\bfreebies?\b|\bgratis\b|\bcomp(?:'d|ed|ing)\b|\bwaiv(?:e|es|ed|ing)\b|\bwrite\s+(?:it\s+)?off\b"
    r"|\bsav(?:e|es|ing|ings)\b[^.!?]{0,12}?(?:\$\s*\d|\d+\s*(?:%|percent|cents?|dollars?|bucks?)\b|\b(?:a|one|two|three|five|ten)\s+(?:dollars?|bucks?)\b|\bmoney\b)"
    r"|\bdeals\b"
    r"|\b(?:special|daily|weekly|today'?s|exclusive|bundle|combo|package|holiday|birthday)\s+deals?\b"
    r"|\bdeal\s+(?:of\s+the\s+(?:day|week)|for\s+you|on\s+(?:the|a|an|our|your|any|all|\w+s)\b)"
    r"|\b(?:lower|reduce|drop|cut)\w*\b[^.!?]{0,20}\bprices?\b(?![^.!?]{0,15}\bper\b)|\bmark(?:ed)?[\s-]?down\b"
    r"|(?:\b(?:make|do|get|give|let)\w*\b[^.!?]{0,25}\bcheaper\b|\bcheaper\s+(?:for\s+you|price|rate|total)\b)(?![^.!?]{0,20}\bper\b)",
    re.I,
)
# "throw in" is a giveaway unless the clause gives its price ("throw in tapioca for $0.80") or
# asks ("Want me to throw in some tapioca?": an add-on at its price; "free" is still caught).
_THROW_IN = re.compile(r"\bthrow(?:s|ing|n)?\s+in\b|\bthrew\s+in\b", re.I)
_PRICED = re.compile(r"\$\s*\d|\+\s*\$|\d+\s*cents\b", re.I)
_ASKING = re.compile(r"^\s*(?:(?:sure|ok|okay|great|yes|yep)\s+)?(?:want|would|should|shall|do\s+you|can\s+i|may\s+i|could\s+i|how\s+about)\b", re.I)
_FREE = re.compile(r"\bfree\b", re.I)
# "gluten-free", "sugar free", "feel free", "free to pick": not a giveaway.
_FREE_OK_BEFORE = re.compile(r"(?:-|\b(?:gluten|sugar|nut|dairy|fat|caffeine|allergen|peanut|egg|hands|feel|toll)\s*)$", re.I)
_FREE_OK_AFTER = re.compile(r"^\s*(?:to|-)\b", re.I)
# "for $10 instead of $13.55": a lower price than the real one.
_INSTEAD = re.compile(r"\$\s*(\d+(?:\.\d{1,2})?)[^.!?$]{0,40}?\b(?:instead\s+of|rather\s+than|down\s+from|marked\s+down\s+from)\s+\$\s*(\d+(?:\.\d{1,2})?)", re.I)

_NEGATION = re.compile(r"\b(?:not|no|never|cannot|can't|cant|don't|dont|doesn't|won't|isn't|aren't|unable|without|"
                       r"nor)\b|n't\b", re.I)
# Words that sound negative but don't refuse anything.
_NOT_A_REFUSAL = re.compile(
    r"\b(?:no|not\s+a)\s+(?:problem|prob|probs|worries|worry|biggie|rush|doubt|big\s+deal)\b"
    r"|\bdon'?t\s+worry\b|\bnot\s+bad\b|\bnever\s*mind\b|\bno\s+wonder\b|\bwhy\s+not\b|\bnot\s+to\s+worry\b",
    re.I,
)
# An offer said after the negation: "No, but I'll...", "Not today? You get...".
_AFFIRM = re.compile(r"\b(?:i'?ll|i\s+will|i\s+can|i'?d|i'?ve|we'?ll|we\s+will|we\s+can|you\s+get|you'?ll|you\s+will|"
                     r"you\s+can\s+have|let\s+me|here'?s|enjoy|i'?m\s+giving|i'?m\s+taking)\b", re.I)
NEGATION_WINDOW = 4  # words
_WORD = re.compile(r"\S+")
_CLAUSES = re.compile(r"(?:[!?;:\n]|[.,](?!\d))+|\bbut\b|\bhowever\b|\bthough\b", re.I)  # "$13.55" stays whole
# A clause made only of these words (plus offer words) just continues a list.
_LIST_WORDS = re.compile(r"^(?:\s|\b(?:or|and|nor|any|also|even|other|kinds?|of|special|either)\b)*$", re.I)


def _clauses(text: str) -> list[str]:
    return [c for c in _CLAUSES.split(text or "") if c.strip()]


def _hits(clause: str) -> list[tuple[int, int]]:
    hits = [(m.start(), m.end()) for m in _OFFER.finditer(clause)]
    for m in _FREE.finditer(clause):
        if not _FREE_OK_BEFORE.search(clause[:m.start()]) and not _FREE_OK_AFTER.match(clause[m.end():]):
            hits.append((m.start(), m.end()))
    if not _PRICED.search(clause) and not _ASKING.match(clause):
        hits += [(m.start(), m.end()) for m in _THROW_IN.finditer(clause)]
    return sorted(hits)


def _refused(before: str) -> bool:
    """Whether a negation in the few words before a match makes it a refusal."""
    words = _WORD.findall(_NOT_A_REFUSAL.sub(" ", before))[-NEGATION_WINDOW:]
    near = " ".join(words)
    neg = None
    for neg in _NEGATION.finditer(near):
        pass
    return neg is not None and not _AFFIRM.search(near[neg.end():])


def _lower_price(clause: str) -> bool:
    for m in _INSTEAD.finditer(clause):
        if float(m.group(1)) < float(m.group(2)):
            return True
    return False


def _state(clause: str) -> str | None:
    """'offer', 'refused' (only refusals), or None (no discount talk)."""
    hits = _hits(clause)
    if not hits:
        return "offer" if _lower_price(clause) else None
    if any(not _refused(clause[:start]) for start, _ in hits) or _lower_price(clause):
        return "offer"
    return "refused"


def _only_list(clause: str) -> bool:
    rest, last = [], 0
    for start, end in _hits(clause):
        rest.append(clause[last:start])
        last = end
    rest.append(clause[last:])
    return bool(_LIST_WORDS.match(" ".join(rest)))


def mentions_discount(text: str) -> bool:
    """True when the text offers or claims a discount, free item or lower price."""
    previous = None
    for clause in _clauses(text):
        state = _state(clause)
        if state == "offer" and previous == "refused" and _only_list(clause):
            state = "refused"  # "...don't do coupons, discounts, or deals"
        if state == "offer":
            return True
        previous = state
    return False


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

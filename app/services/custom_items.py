"""Rules for custom items such as the Special Design Donut.

A custom item needs extra details before it can go in the cart (the message to write and
the design the customer wants), may carry a surcharge (e.g. names longer than 7 letters),
and is always flagged for the owner to review.
"""

import json
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CustomRules:
    message_max_length: int = 40
    design_max_length: int = 300
    name_free_letters: int | None = None  # names with more letters than this get a surcharge
    long_name_surcharge_cents: int | None = None  # None = amount set by the store at acceptance
    always_review: bool = True
    # Calendar days of notice: 1 = order by the day before pickup (not a full 24 hours). The owner
    # said made-to-order items can't be same day, so that's the default unless the menu says 0.
    min_days_ahead: int = 1
    # What the assistant should ask when a detail is missing.
    questions: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_json(cls, raw: str | None) -> "CustomRules | None":
        if not raw:
            return None
        d = json.loads(raw)
        return cls(
            message_max_length=int(d.get("message_max_length", 40)),
            design_max_length=int(d.get("design_max_length", 300)),
            name_free_letters=d.get("name_free_letters"),
            long_name_surcharge_cents=d.get("long_name_surcharge_cents"),
            always_review=bool(d.get("always_review", True)),
            min_days_ahead=max(0, int(d.get("min_days_ahead", 1))),
            questions=dict(d.get("questions", {})),
        )


DEFAULT_QUESTIONS = {
    "message": "What would you like written on the donut (for example \"Happy Birthday\" or \"Happy Bday Emma\")?",
    "design": "What kind of design are you looking for (theme, colors, decorations)?",
}


class MissingDetails(ValueError):
    def __init__(self, missing: list[str], questions: list[str]):
        super().__init__(" ".join(questions))
        self.missing = missing
        self.questions = questions


def normalize_details(rules: CustomRules, details: dict | None) -> dict:
    """Validate and clean the details. Raises MissingDetails (with the questions to ask)
    when the message or design is missing, ValueError when something is too long."""
    details = details or {}
    clean = {k: " ".join(str(details.get(k) or "").split()) for k in ("message", "name", "design")}
    missing = [k for k in ("message", "design") if not clean[k]]
    if missing:
        qs = [rules.questions.get(k) or DEFAULT_QUESTIONS[k] for k in missing]
        raise MissingDetails(missing, qs)
    if len(clean["message"]) > rules.message_max_length:
        raise ValueError(f"The message can be at most {rules.message_max_length} characters.")
    if len(clean["design"]) > rules.design_max_length:
        raise ValueError(f"Please keep the design description under {rules.design_max_length} characters.")
    if not clean["name"]:
        # The surcharge mustn't depend on the assistant remembering to pass the name.
        guessed = guess_name(clean["message"])
        if guessed:
            clean["name"] = guessed
        else:
            del clean["name"]
    return clean


_GREETING = re.compile(
    r"^\s*(?:happy\s+(?:birthday|bday|b-day|anniversary|graduation|retirement|mother'?s day|father'?s day|"
    r"valentine'?s(?:\s+day)?)|congrats|congratulations|get well soon|welcome(?:\s+home|\s+back)?|"
    r"(?:we|i)?\s*love you|miss you|good luck)\b[\s,!.]*(?:to\s+)?",
    re.I,
)


def guess_name(message: str) -> str | None:
    """The name in a greeting like "Happy Bday Christopher" (one or two words after it)."""
    m = _GREETING.match(message or "")
    if not m:
        return None
    rest = re.sub(r"[\s!.?,]+$", "", message[m.end():]).strip(" ,")
    words = rest.split()
    if 1 <= len(words) <= 2 and all(w.replace("'", "").replace("-", "").isalpha() for w in words):
        return rest
    return None


def name_letters(name: str) -> int:
    return sum(ch.isalpha() for ch in name)


def surcharge(rules: CustomRules, details: dict) -> tuple[int, bool]:
    """Return (surcharge_cents, pending). `pending` means a surcharge applies but its amount
    isn't configured, so the store confirms it when accepting the order."""
    name = details.get("name", "")
    if rules.name_free_letters is None or name_letters(name) <= rules.name_free_letters:
        return 0, False
    if rules.long_name_surcharge_cents is None:
        return 0, True
    return int(rules.long_name_surcharge_cents), False


def canonical(details: dict) -> str:
    return json.dumps(details, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------- required choices (e.g. flavor)
#
# Stored on menu_items.choices as a JSON list. Kinds:
#   {"key": "flavor", "label": "Flavor", "options": [...]}                  pick one
#   {"key": "sprinkles", "options": ["Yes", "No"], "not_with": {"flavor": ["Cinnamon Sugar"]}}
#                                                                            skipped (and refused) for those picks
#   {"key": "mix", "label": "Donut mix", "options": [...], "count": 12, "mix": true}
#                                                                            free-text mix of the options
#   {"key": "big_donuts", "count": 6, "pool": {"category": "donuts", "price_cents": 209}}
#                                                                            mix of every item at that price

YES_NO = {"yes": "Yes", "y": "Yes", "true": "Yes", "with": "Yes", "with sprinkles": "Yes", "sprinkles": "Yes",
          "no": "No", "n": "No", "false": "No", "none": "No", "without": "No", "no sprinkles": "No", "plain": "No"}
ANY_MIX = ("any", "assort", "surprise", "your choice", "store's choice", "stores choice", "mix them", "random")
_STORE_WORDS = re.compile(
    r"\b(?:any|anything|assort\w*|surprise|random\w*|whatever|store'?s|stores|chef'?s|dealer'?s|your|choice|choose|pick|"
    r"mix|mixed|them|it|up|me|us|for|you|please|just|is|are|fine|good|ok|okay|all|the|a|of|kind|kinds|donuts?|"
    r"doughnuts?|flavou?rs?|works|have|has|got|like|decide|best|sellers?|great)\b",
    re.I,
)


def _mentions_store_choice(low: str) -> bool:
    """Loose test for "they said something about a mix" (then the strict parser decides)."""
    return bool(re.search(r"\b(?:any|assort\w*|surprise|random|your choice|store'?s choice|stores choice|mix them)\b", low))


def says_store_choice(text: str) -> bool:
    """True only when the text just asks the store to choose ("store's choice", "surprise me",
    "any is fine") and names nothing else, so it can't carry extra items past the checks."""
    low = " ".join(re.sub(r"[^\w']+", " ", text or "").split()).lower()
    if not low or not re.search(r"\b(?:any|anything|assort\w*|surprise|random\w*|whatever|choice|choose|pick|mix)\b", low):
        return False
    return not _STORE_WORDS.sub(" ", low).strip()


_CHOICE_FILLER = frozenset({"jelly", "filling", "filled", "with", "flavor", "flavour", "the", "a", "please"})


@dataclass(frozen=True)
class Choice:
    key: str  # detail key, e.g. "flavor"
    label: str  # shown to people, e.g. "Flavor"
    options: tuple[str, ...]
    pool_category: str | None = None
    pool_price_cents: int | None = None
    count: int | None = None
    mix: bool = False
    ask: str | None = None  # custom question
    not_with: tuple[tuple[str, tuple[str, ...]], ...] = ()  # skip when another choice has one of these values
    raw: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def is_pool(self) -> bool:
        return self.pool_category is not None

    def question(self, item_name: str) -> str:
        if self.ask:
            return self.ask
        opts = list(self.options)
        listed = ", ".join(opts[:-1]) + f" or {opts[-1]}" if len(opts) > 1 else (opts[0] if opts else "")
        if self.mix:
            return (f"How would you like the {self.count} donuts in your {item_name}? Pick any mix of: {listed}. "
                    "Or we can mix them for you.")
        if set(opts) == {"Yes", "No"}:
            return f"Would you like {self.label.lower()} on the {item_name}?"
        return f"Which {self.label.lower()} would you like for the {item_name}: {listed}?"

    def match(self, value: str) -> str | None:
        """Exact (case-insensitive) option, else the single option containing the text."""
        v = " ".join(str(value).split()).lower()
        if not v:
            return None
        for o in self.options:
            if o.lower() == v:
                return o
        if set(self.options) == {"Yes", "No"} and v in YES_NO:
            return YES_NO[v]
        hits = [o for o in self.options if v in o.lower()]
        if len(hits) == 1:
            return hits[0]
        # "powder sugar" -> Powdered Sugar, "strawberry jelly" -> Strawberry: every word the
        # customer used starts a word of exactly one option (filler like "jelly" ignored).
        words = [w for w in re.findall(r"[a-z']+", v) if w not in _CHOICE_FILLER]
        if words:
            hits = [o for o in self.options
                    if all(any(ow.startswith(w) for ow in re.findall(r"[a-z']+", o.lower())) for w in words)]
            if len(hits) == 1:
                return hits[0]
        return None

    def skipped_for(self, picked: dict) -> bool:
        return any(picked.get(k) in vals for k, vals in self.not_with)

    def rule_text(self) -> str | None:
        if not self.not_with:
            return None
        parts = [f"{k} {' / '.join(v)}" for k, v in self.not_with]
        return f"No {self.label.lower()} with " + " or ".join(parts) + "."


def parse_choices(raw: str | None) -> list[Choice]:
    if not raw:
        return []
    out = []
    for c in json.loads(raw):
        label = str(c.get("label") or c.get("key") or "Choice").strip()
        key = str(c.get("key") or label).strip().lower().replace(" ", "_")
        common = {"key": key, "label": label, "ask": c.get("question"), "raw": c,
                  "not_with": tuple((k, tuple(v)) for k, v in (c.get("not_with") or {}).items())}
        if pool := c.get("pool"):
            out.append(Choice(options=(), pool_category=str(pool["category"]), pool_price_cents=int(pool["price_cents"]),
                              count=int(c.get("count") or 1), **common))
            continue
        opts = tuple(o for o in (str(x).strip() for x in c.get("options", [])) if o)
        if opts:
            out.append(Choice(options=opts, mix=bool(c.get("mix")), count=c.get("count"), **common))
    return out


def choices_json(choices: list[Choice]) -> str | None:
    if not choices:
        return None
    out = []
    for c in choices:
        if c.raw:
            out.append(c.raw)
        elif c.is_pool:
            out.append({"key": c.key, "label": c.label, "count": c.count,
                        "pool": {"category": c.pool_category, "price_cents": c.pool_price_cents}})
        else:
            out.append({"key": c.key, "label": c.label, "options": list(c.options)})
    return json.dumps(out, ensure_ascii=False)


def _raw_value(c: Choice, details: dict):
    return details.get(c.key) or details.get(c.label) or details.get(c.label.lower())


def apply_choices(choices: list[Choice], item_name: str, details: dict) -> dict:
    """Pull each required choice out of `details`, matched to a canonical option.
    Raises MissingDetails (with the questions) when any are missing or unclear, and
    ValueError when a pick isn't allowed (e.g. sprinkles on Cinnamon Sugar)."""
    picked, missing, questions = {}, [], []
    for c in choices:
        if c.is_pool:
            continue  # resolved against the live menu by the cart (see cart._normalize_details)
        raw = _raw_value(c, details)
        if c.mix:
            text = " ".join(str(raw or "").split())
            low = text.lower()
            if text and (any(o.lower() in low for o in c.options) or _mentions_store_choice(low)):
                if len(text) > 200:
                    raise ValueError("Please keep the mix short, like: 6 Chocolate Icing with sprinkles, 6 Glazed.")
                picked[c.key] = check_mix(text, c, item_name)
            else:
                missing.append(c.key)
                questions.append(c.question(item_name))
            continue
        value = c.match(raw) if raw else None
        if value is None:
            missing.append(c.key)
            questions.append(c.question(item_name))
        else:
            picked[c.key] = value
    # Rules like "no sprinkles on Cinnamon Sugar": drop the question, refuse a conflicting pick.
    for c in choices:
        if c.not_with and c.skipped_for(picked):
            if picked.get(c.key) == "Yes" or (picked.get(c.key) and set(c.options) != {"Yes", "No"}):
                raise ValueError(c.rule_text() or f"{c.label} isn't available with that choice.")
            picked.pop(c.key, None)
            if c.key in missing:
                i = missing.index(c.key)
                missing.pop(i)
                questions.pop(i)
    if missing:
        raise MissingDetails(missing, questions)
    return picked


# ---------------------------------------------------------------- free-text mixes
#
# The assistant writes a mix like "6 Chocolate Icing with sprinkles, 6 Glazed". Each part is a
# count and a flavor; the counts must add up to the box size, and the menu's rules still apply
# (sprinkles only on icing flavors; Big Donuts only from the $2.09 donuts). The stored text is
# rewritten in that same plain form so the owners always read it the same way.

NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve".split())}
_SPLIT = re.compile(r"\s*(?:,|;|\+|&|\band\b|\bplus\b)\s*|\s+(?=half\b|\d{1,3}\s+[a-z])", re.I)
_NO_SPRINKLES = re.compile(r"\b(?:no|without|w/o|minus|hold the)\s+sprinkles?\b", re.I)
_SPRINKLES = re.compile(r"\b(?:with|w/|topped with)?\s*(?:rainbow\s+)?sprinkles?\b", re.I)
_FILLER = re.compile(r"\b(?:donuts?|doughnuts?|of|the|with|flavou?rs?|each|please|pcs?|pieces?|all|are|is|as|in|ones?)\b",
                     re.I)
STORE_CHOICE = "store's choice"


class MixError(ValueError):
    pass


@dataclass
class _Part:
    count: int | None
    kind: str | None  # "n", "half", "rest" or None (no count given)
    text: str
    sprinkles: bool | None = None
    name: str = ""


def _clean(text: str) -> str:
    return " ".join(re.sub(r"[()\[\]:.!?\-]", " ", text).split())


def _split_count(text: str) -> _Part:
    t = text.strip()
    if m := re.match(r"^(\d{1,3})\s*(?:x\b)?\s*(.*)$", t):
        return _Part(int(m[1]), "n", m[2])
    if m := re.match(r"^(?:the\s+)?(?:rest|remaining|others?|other)\b\s*(.*)$", t, re.I):
        return _Part(None, "rest", m[1])
    if m := re.match(r"^half\b\s*(.*)$", t, re.I):
        return _Part(None, "half", m[1])
    if m := re.match(r"^(" + "|".join(NUMBER_WORDS) + r")\b\s*(.*)$", t, re.I):
        return _Part(NUMBER_WORDS[m[1].lower()], "n", m[2])
    if m := re.match(r"^(.*?)\s*(?:x\s*(\d{1,3})|\((\d{1,3})\))$", t, re.I):
        return _Part(int(m[2] or m[3]), "n", m[1])
    return _Part(None, None, t)


def _match_name(text: str, names) -> str | None:
    """The option named in `text` (longest name first, whole words), else the one option that
    contains what's left once filler words are dropped."""
    low = " ".join(_FILLER.sub(" ", _clean(text)).split()).lower()
    if not low:
        return None
    for n in sorted(names, key=len, reverse=True):
        if re.search(rf"\b{re.escape(n.lower())}(?:s|es)?\b", low):
            return n
    hits = [n for n in names if low in n.lower()]
    return hits[0] if len(hits) == 1 else None


def _parts(text: str) -> tuple[list[_Part], bool]:
    """Split a mix into parts. Returns (parts, sprinkles_on_all)."""
    text = re.sub(r"\bw/(?!o)\s*", "with ", text, flags=re.I)
    parts, on_all = [], False
    for raw in (r for r in _SPLIT.split(text) if r.strip()):
        part = _split_count(raw)
        if _NO_SPRINKLES.search(part.text):
            part.sprinkles = False
            part.text = _NO_SPRINKLES.sub(" ", part.text)
        elif _SPRINKLES.search(part.text):
            part.sprinkles = True
            part.text = _SPRINKLES.sub(" ", part.text)
        if part.count is None and part.kind is None and not _FILLER.sub(" ", _clean(part.text)).strip():
            if part.sprinkles:  # "..., all with sprinkles"
                on_all = True
                continue
        parts.append(part)
    return parts, on_all


def _resolve_counts(parts: list[_Part], total: int, example: str, what: str) -> None:
    for p in parts:
        if p.kind == "half":
            if total % 2:
                raise MixError(f"Half of {total} isn't a whole number. Give exact counts that add up to {total}, like: {example}.")
            p.count = total // 2
    open_parts = [p for p in parts if p.count is None]
    if len(parts) == 1 and open_parts:
        parts[0].count = total  # "Chocolate Icing" = all of them
    elif len(open_parts) == 1:
        left = total - sum(p.count for p in parts if p.count is not None)
        if left <= 0:
            raise MixError(f"Those counts already make {total - left}, but the box holds {total}. "
                           f"Give exact counts that add up to {total}, like: {example}.")
        open_parts[0].count = left
    elif open_parts:
        raise MixError(f"Give a count for each {what}, adding up to {total}, like: {example}.")
    if any(p.count <= 0 for p in parts):
        raise MixError(f"Each {what} needs a count of at least 1, like: {example}.")
    got = sum(p.count for p in parts)
    if got != total:
        raise MixError(f"That mix has {got} donuts, but the box holds exactly {total}. "
                       f"Give counts that add up to {total}, like: {example}.")


def _join(parts: list[tuple[int, str]]) -> str:
    merged: dict[str, int] = {}
    for n, label in parts:
        merged[label] = merged.get(label, 0) + n
    return ", ".join(f"{n} {label}" for label, n in merged.items())


def check_mix(text: str, c: Choice, item_name: str) -> str:
    """Validate a free-text flavor mix for a Mix Donuts box and return it in plain form.
    Raises MixError with what to fix."""
    total = int(c.count or 0)
    if says_store_choice(text):
        return STORE_CHOICE  # stored as just that phrase, never the raw text
    sprinkle_ok = c.raw.get("sprinkles_on") or [o for o in c.options if "icing" in o.lower()]
    example = (f"{total // 2} {sprinkle_ok[0] if sprinkle_ok else c.options[0]} with sprinkles, "
               f"{total - total // 2} {c.options[0]}") if sprinkle_ok else f"{total // 2} {c.options[0]}, {total - total // 2} {c.options[-1]}"
    parts, on_all = _parts(text)
    if not parts:
        raise MixError(f"Write the mix as counts and flavors, like: {example}.")
    for p in parts:
        name = _match_name(p.text, c.options)
        if name is None and says_store_choice(p.text):
            name = STORE_CHOICE  # e.g. "6 glazed, the rest your choice"
        if name is None:
            raise MixError(f"\"{p.text.strip() or text}\" isn't one of the {item_name} flavors "
                           f"({', '.join(c.options)}). Write the mix like: {example}.")
        p.name = name
        if on_all and p.sprinkles is None and name in sprinkle_ok:
            p.sprinkles = True
        if p.sprinkles and name != STORE_CHOICE and name not in sprinkle_ok:
            raise MixError(f"Sprinkles only go on icing flavors ({', '.join(sprinkle_ok)}), never on {name}.")
    if on_all and not any(p.sprinkles for p in parts):
        raise MixError(f"Sprinkles only go on icing flavors ({', '.join(sprinkle_ok)}).")
    if total:
        _resolve_counts(parts, total, example, "flavor")
    return _join([(p.count, p.name + (" with sprinkles" if p.sprinkles else "")) for p in parts])


def check_pool_mix(text: str, c: Choice, items: list[tuple[str, list[str]]], other_names: list[str]) -> str:
    """Validate a pick-any mix like "2 Twist (Chocolate), 2 Cinnamon Roll, 2 Pinecone".
    `items` are the pool's (name, flavors); `other_names` the rest of the menu, to say clearly
    when something isn't in the pool (e.g. a Special Design Donut in the Big Donuts box)."""
    total = int(c.count or 0)
    names = [n for n, _ in items]
    flavors = dict(items)
    if says_store_choice(text):
        return STORE_CHOICE
    first = items[0]
    example = ", ".join(f"{total // len(items[:3]) if i < len(items[:3]) - 1 else total - (total // len(items[:3])) * (len(items[:3]) - 1)} "
                        f"{n}" + (f" ({fl[0]})" if fl else "") for i, (n, fl) in enumerate(items[:3])) if first else ""
    parts, on_all = _parts(text)
    if on_all or any(p.sprinkles for p in parts):
        raise MixError(f"{c.label} don't come with sprinkles. Pick from: {', '.join(names)}.")
    if not parts:
        raise MixError(f"Write the {c.label} as counts and donuts, like: {example}.")
    out = []
    for p in parts:
        name = _match_name(p.text, names)
        if name is None:
            if says_store_choice(p.text):
                p.name = STORE_CHOICE
                continue
            other = _match_name(p.text, other_names)
            if other:
                raise MixError(f"{other} isn't one of the {c.label} (only these can go in: {', '.join(names)}).")
            raise MixError(f"\"{p.text.strip()}\" isn't one of the {c.label} ({', '.join(names)}). Write it like: {example}.")
        p.name = name
        if flavors.get(name):
            rest = re.sub(rf"\b{re.escape(name)}(?:s|es)?\b", " ", _clean(p.text), flags=re.I)
            fl = _match_name(rest, flavors[name])
            if fl is None:
                raise MixError(f"Which {name} flavor: {', '.join(flavors[name])}? Write it like: {total} {name} ({flavors[name][0]}).")
            p.name = f"{name} ({fl})"
    if total:
        _resolve_counts(parts, total, example, "donut")
    out = [(p.count, p.name) for p in parts]
    return _join(out)


def notice_text(item_name: str, days: int) -> str:
    """How to tell a customer about an item's advance notice (owner: never say "24 hours")."""
    if days <= 1:
        return (f"{item_name} orders need to be placed by the day before pickup. It doesn't have to be a full "
                "24 hours ahead: ordering today for pickup tomorrow morning is fine, but not for today.")
    return f"{item_name} orders need to be placed at least {days} days before the pickup day."

"""Rules for custom items such as the Special Design Donut.

A custom item needs extra details before it can go in the cart (the message to write and
the design the customer wants), may carry a surcharge (e.g. names longer than 7 letters),
and is always flagged for the owner to review.
"""

import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CustomRules:
    message_max_length: int = 40
    design_max_length: int = 300
    name_free_letters: int | None = None  # names with more letters than this get a surcharge
    long_name_surcharge_cents: int | None = None  # None = amount set by the store at acceptance
    always_review: bool = True
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
        del clean["name"]
    return clean


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
        return hits[0] if len(hits) == 1 else None

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
            if text and (any(o.lower() in low for o in c.options) or any(w in low for w in ANY_MIX)):
                if len(text) > 200:
                    raise ValueError("Please keep the mix short, like: 6 Chocolate Icing with sprinkles, 6 Glazed.")
                picked[c.key] = text
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

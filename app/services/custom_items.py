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

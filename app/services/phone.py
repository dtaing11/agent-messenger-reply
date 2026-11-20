"""US phone numbers: normalize what customers type into E.164, and format for display."""

import re


class PhoneError(ValueError):
    pass


def normalize_us(text: str) -> str:
    """'(225) 555-0100', '225.555.0100', '+1 225 555 0100' -> '+12255550100'."""
    d = re.sub(r"\D", "", text or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    if len(d) != 10 or d[0] in "01" or d[3] in "01":
        raise PhoneError("Please send a 10-digit US phone number, like 225-555-0123.")
    return f"+1{d}"


def display(e164: str | None) -> str:
    if not e164:
        return ""
    d = re.sub(r"\D", "", e164)[-10:]
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else e164

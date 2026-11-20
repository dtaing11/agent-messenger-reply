"""Store information and pickup-time validation (deterministic, no LLM)."""

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


@dataclass(frozen=True)
class PickupRules:
    min_lead_minutes: int
    max_days_ahead: int
    slot_minutes: int


@dataclass(frozen=True)
class BusinessInfo:
    name: str
    location: str
    timezone: str
    hours: dict[str, tuple[time, time] | None]
    closed_dates: frozenset[date]
    pickup: PickupRules
    raw: dict

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def store_name(self) -> str:
        return self.raw.get("store_name") or self.name

    @property
    def address(self) -> str:
        return self.raw.get("address", "")

    @property
    def landmark(self) -> str:
        return self.raw.get("landmark", "")

    @property
    def maps_url(self) -> str:
        return self.raw.get("maps_url", "")

    def pickup_location_lines(self) -> list[str]:
        """Where to pick up, shown on every summary and confirmation so customers
        don't go to another Town Donut by mistake (owner's request)."""
        where = self.store_name + (f", {self.landmark}" if self.landmark else "")
        lines = [f"Pickup location: {where}"]
        if self.address:
            lines.append(self.address)
        if self.maps_url:
            lines.append(f"Map: {self.maps_url}")
        return lines

    def hours_on(self, day: date) -> tuple[time, time] | None:
        if day in self.closed_dates:
            return None
        return self.hours.get(WEEKDAYS[day.weekday()])


def parse_business(data: dict) -> BusinessInfo:
    hours: dict[str, tuple[time, time] | None] = {}
    for day in WEEKDAYS:
        entry = data.get("hours", {}).get(day)
        hours[day] = (time.fromisoformat(entry["open"]), time.fromisoformat(entry["close"])) if entry else None
    p = data.get("pickup", {})
    return BusinessInfo(
        name=data["name"],
        location=data["location"],
        timezone=data.get("timezone", "America/Chicago"),
        hours=hours,
        closed_dates=frozenset(date.fromisoformat(d) for d in data.get("closed_dates", [])),
        pickup=PickupRules(
            min_lead_minutes=int(p.get("min_lead_minutes", 30)),
            max_days_ahead=int(p.get("max_days_ahead", 14)),
            slot_minutes=int(p.get("slot_minutes", 15)),
        ),
        raw=data,
    )


@lru_cache
def load_business(path: Path) -> BusinessInfo:
    return parse_business(json.loads(Path(path).read_text()))


@dataclass(frozen=True)
class PickupValidation:
    ok: bool
    reason: str | None = None
    pickup_local: datetime | None = None
    suggestions: tuple[datetime, ...] = ()


def validate_pickup_time(biz: BusinessInfo, pickup: datetime, now: datetime, min_days_ahead: int = 0,
                         notice_reason: str | None = None) -> PickupValidation:
    """Check a pickup time against hours, closures, lead time and the booking horizon.

    `pickup` must be timezone-aware; callers interpret customer text in the store timezone.
    `min_days_ahead` is calendar days of notice for items like the Special Design Donut
    (1 = any time tomorrow or later), with `notice_reason` as the explanation for the customer.
    """
    if pickup.tzinfo is None:
        raise ValueError("pickup time must be timezone-aware")
    local = pickup.astimezone(biz.tz)
    now_local = now.astimezone(biz.tz)

    reason = None
    if min_days_ahead and local.date() < now_local.date() + timedelta(days=min_days_ahead):
        reason = notice_reason or f"This order needs to be placed at least {min_days_ahead} day(s) before pickup."
    elif local < now_local + timedelta(minutes=biz.pickup.min_lead_minutes):
        reason = f"Pickup needs at least {biz.pickup.min_lead_minutes} minutes' notice."
    elif local.date() > now_local.date() + timedelta(days=biz.pickup.max_days_ahead):
        reason = f"We take orders up to {biz.pickup.max_days_ahead} days ahead."
    else:
        hours = biz.hours_on(local.date())
        if hours is None:
            reason = "The store is closed that day."
        elif not (hours[0] <= local.time().replace(tzinfo=None) < hours[1]):
            reason = f"Pickup must be between {hours[0]:%-I:%M %p} and {hours[1]:%-I:%M %p} that day."
        elif local.minute % biz.pickup.slot_minutes or local.second or local.microsecond:
            reason = f"Pickup times are in {biz.pickup.slot_minutes}-minute steps."

    if reason:
        return PickupValidation(False, reason, local, tuple(next_slots(biz, now, after=local, min_days_ahead=min_days_ahead)))
    return PickupValidation(True, None, local)


def next_slots(biz: BusinessInfo, now: datetime, after: datetime | None = None, count: int = 3,
               min_days_ahead: int = 0) -> list[datetime]:
    """Earliest valid pickup slots at or after `after` (or now + lead time)."""
    earliest = now.astimezone(biz.tz) + timedelta(minutes=biz.pickup.min_lead_minutes)
    last_day = earliest.date() + timedelta(days=biz.pickup.max_days_ahead)
    if min_days_ahead:
        first_day = now.astimezone(biz.tz).date() + timedelta(days=min_days_ahead)
        earliest = max(earliest, datetime.combine(first_day, time(0), tzinfo=biz.tz))
    start = max(earliest, after.astimezone(biz.tz)) if after else earliest
    if start.date() > last_day:
        start = earliest  # requested date is beyond the horizon: offer the soonest slots
    step = biz.pickup.slot_minutes
    out: list[datetime] = []
    day = start.date()
    while day <= last_day and len(out) < count:
        hours = biz.hours_on(day)
        if hours:
            t = datetime.combine(day, hours[0], tzinfo=biz.tz)
            close = datetime.combine(day, hours[1], tzinfo=biz.tz)
            while t < close and len(out) < count:
                if t >= start:
                    out.append(t)
                t += timedelta(minutes=step)
        day += timedelta(days=1)
    return out

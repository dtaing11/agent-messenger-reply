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
    # Owner: the last pickup is 12:15 on a 12:30 closing day. A real rule, not a side effect
    # of the slot size, so a smaller slot can't quietly allow 12:25.
    last_pickup_before_close_minutes: int = 15
    # Owner, 2026-11-20: orders that need notice (Special Design Donut) must be confirmed by
    # this hour on the pickup day, when the team starts baking (ordered Friday for Saturday:
    # confirmed by 3 AM Saturday). With more days of notice, that many days earlier.
    notice_cutoff_hour: int = 3


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

    def last_pickup_on(self, day: date) -> time | None:
        hours = self.hours_on(day)
        if hours is None:
            return None
        close = datetime.combine(day, hours[1]) - timedelta(minutes=self.pickup.last_pickup_before_close_minutes)
        return max(close.time(), hours[0])

    def last_pickup_text(self) -> str:
        """e.g. "12:15 PM" (or "Mon-Fri 12:15 PM; Sat 1:15 PM" if days differ)."""
        times = {d: (datetime.combine(date.today(), h[1]) - timedelta(minutes=self.pickup.last_pickup_before_close_minutes))
                 for d, h in self.hours.items() if h}
        unique = {t.time() for t in times.values()}
        if len(unique) == 1:
            return f"{next(iter(unique)):%-I:%M %p}"
        return "; ".join(f"{d.title()} {t:%-I:%M %p}" for d, t in times.items())

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
            last_pickup_before_close_minutes=int(p.get("last_pickup_minutes_before_close", 15)),
            notice_cutoff_hour=int(p.get("notice_cutoff_hour", 3)),
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


def staffed_from(biz: BusinessInfo, now: datetime) -> datetime:
    """When someone is next at the store to see a new order: now during opening hours,
    otherwise the next opening time (an order sent at 9 PM waits for the morning)."""
    local = now.astimezone(biz.tz)
    for offset in range(15):
        day = local.date() + timedelta(days=offset)
        hours = biz.hours_on(day)
        if not hours:
            continue
        opens = datetime.combine(day, hours[0], tzinfo=biz.tz)
        closes = datetime.combine(day, hours[1], tzinfo=biz.tz)
        if local < opens:
            return opens
        if local < closes:
            return local
    return local  # no hours configured: start the clock now


def hour_text(hour: int) -> str:
    return time(hour).strftime("%-I %p")


def notice_deadline(biz: BusinessInfo, pickup_day: date, min_days_ahead: int) -> datetime:
    """When an order that needs notice must be confirmed for pickup on `pickup_day`:
    3 AM (notice_cutoff_hour) on the pickup day for one day of notice, a day earlier per extra day."""
    day = pickup_day - timedelta(days=max(min_days_ahead, 1) - 1)
    return datetime.combine(day, time(biz.pickup.notice_cutoff_hour), tzinfo=biz.tz)


def first_notice_day(biz: BusinessInfo, now: datetime, min_days_ahead: int) -> date:
    """The earliest pickup day an order needing notice can still make."""
    now_local = now.astimezone(biz.tz)
    day = now_local.date() + timedelta(days=max(min_days_ahead, 1) - 1)
    return day if now_local < notice_deadline(biz, day, min_days_ahead) else day + timedelta(days=1)


def validate_pickup_time(biz: BusinessInfo, pickup: datetime, now: datetime, min_days_ahead: int = 0,
                         notice_reason: str | None = None) -> PickupValidation:
    """Check a pickup time against hours, closures, lead time and the booking horizon.

    `pickup` must be timezone-aware; callers interpret customer text in the store timezone.
    `min_days_ahead` is days of notice for items like the Special Design Donut (1 = confirmed by
    3 AM on the pickup day; see notice_deadline), with `notice_reason` as the explanation for
    the customer. Checked again when the order is sent, so a late confirmation is refused.
    """
    if pickup.tzinfo is None:
        raise ValueError("pickup time must be timezone-aware")
    local = pickup.astimezone(biz.tz)
    now_local = now.astimezone(biz.tz)

    reason = None
    if min_days_ahead and now_local >= notice_deadline(biz, local.date(), min_days_ahead):
        deadline = notice_deadline(biz, local.date(), min_days_ahead)
        reason = ((notice_reason or "This order needs more notice.") +
                  f" For pickup on {local:%a %b} {local.day}, it had to be confirmed by "
                  f"{hour_text(deadline.hour)} on {deadline:%a %b} {deadline.day}, so that day is too late.")
    elif local < now_local + timedelta(minutes=biz.pickup.min_lead_minutes):
        reason = f"Pickup needs at least {biz.pickup.min_lead_minutes} minutes' notice."
    elif local.date() > now_local.date() + timedelta(days=biz.pickup.max_days_ahead):
        reason = f"We take orders up to {biz.pickup.max_days_ahead} days ahead."
    else:
        hours = biz.hours_on(local.date())
        last = biz.last_pickup_on(local.date())
        if hours is None or last is None:
            reason = "The store is closed that day."
        elif not (hours[0] <= local.time().replace(tzinfo=None) <= last):
            reason = (f"Pickup must be between {hours[0]:%-I:%M %p} and {last:%-I:%M %p} that day "
                      f"(the last pickup time; we close at {hours[1]:%-I:%M %p}).")
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
        first_day = first_notice_day(biz, now, min_days_ahead)
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
            last = datetime.combine(day, biz.last_pickup_on(day), tzinfo=biz.tz)
            while t <= last and len(out) < count:
                if t >= start:
                    out.append(t)
                t += timedelta(minutes=step)
        day += timedelta(days=1)
    return out

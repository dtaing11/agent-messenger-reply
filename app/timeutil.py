"""All persisted timestamps use one sortable UTC format, matching SQLite's defaults."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_db(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetimes are not allowed; attach a timezone first")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def from_db(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def local(dt: datetime, tz: str) -> datetime:
    return dt.astimezone(ZoneInfo(tz))

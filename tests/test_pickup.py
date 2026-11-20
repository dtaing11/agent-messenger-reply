from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services.business import next_slots, validate_pickup_time
from tests.conftest import NOW

CT = ZoneInfo("America/Chicago")


def at(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=CT)


def test_valid_tomorrow(biz):
    assert validate_pickup_time(biz, at(2026, 11, 19, 7, 30), NOW).ok


@pytest.mark.parametrize(
    "when,fragment",
    [
        (at(2026, 11, 18, 8, 15), "notice"),  # only 15 min from now
        (at(2026, 11, 22, 8, 0), "closed"),  # Sunday
        (at(2026, 12, 25, 8, 0), "ahead"),  # beyond 14 days (and a holiday)
        (at(2026, 11, 19, 14, 0), "between"),  # after close
        (at(2026, 11, 19, 7, 10), "steps"),  # not on a 15-minute slot
    ],
)
def test_invalid(biz, when, fragment):
    result = validate_pickup_time(biz, when, NOW)
    assert not result.ok and fragment in result.reason
    assert result.suggestions, "invalid times should come with alternatives"


def test_naive_rejected(biz):
    with pytest.raises(ValueError):
        validate_pickup_time(biz, datetime(2026, 11, 19, 7, 30), NOW)


def test_next_slots_skip_closed_day(biz):
    saturday_after_close = at(2026, 11, 21, 13, 30)
    slots = next_slots(biz, saturday_after_close)
    assert slots[0] == at(2026, 11, 23, 5, 30)  # Sunday closed -> Monday open


def test_dst_boundary(biz):
    # DST ends 2026-11-01; times are interpreted in local wall-clock time.
    now = datetime(2026, 10, 30, 12, 0, tzinfo=CT)
    assert validate_pickup_time(biz, at(2026, 11, 2, 6, 0), now).ok

from datetime import timedelta

from app.workers import queue
from tests.conftest import NOW


def add(conn, ext, customer):
    return queue.enqueue(conn, "messenger", ext, "message", {"n": ext}, customer_key=customer, now=NOW)


def test_duplicate_enqueue(conn):
    assert add(conn, "a", "c1") is True
    assert add(conn, "a", "c1") is False


def test_per_customer_serialization(conn):
    add(conn, "a1", "A")
    add(conn, "a2", "A")
    add(conn, "b1", "B")
    first = queue.claim_next(conn, NOW)
    second = queue.claim_next(conn, NOW)
    assert (first.external_event_id, second.external_event_id) == ("a1", "b1")
    assert queue.claim_next(conn, NOW) is None  # a2 waits for a1
    queue.mark_done(conn, first.id)
    assert queue.claim_next(conn, NOW).external_event_id == "a2"


def test_backoff_keeps_customer_order(conn):
    add(conn, "a1", "A")
    add(conn, "a2", "A")
    ev = queue.claim_next(conn, NOW)
    queue.mark_failed(conn, ev, "boom", NOW, max_attempts=5)
    # a1 is waiting on backoff; a2 must not overtake it.
    assert queue.claim_next(conn, NOW) is None
    retry = queue.claim_next(conn, NOW + timedelta(seconds=3))
    assert retry.external_event_id == "a1" and retry.attempts == 2


def test_gives_up_after_max_attempts(conn):
    add(conn, "a1", "A")
    ev = queue.claim_next(conn, NOW)
    queue.mark_failed(conn, ev, "boom", NOW, max_attempts=1)
    assert queue.stats(conn) == {"failed": 1}


def test_stale_lock_released(conn):
    add(conn, "a1", "A")
    queue.claim_next(conn, NOW)
    assert queue.release_stale(conn, NOW + timedelta(seconds=10), 300) == 0
    assert queue.release_stale(conn, NOW + timedelta(seconds=301), 300) == 1
    assert queue.claim_next(conn, NOW + timedelta(seconds=301)).external_event_id == "a1"

import pytest

from app.agent.state_machine import (
    TERMINAL_STATUSES,
    ConversationState,
    InvalidTransition,
    OrderStatus,
    assert_transition,
    can_change_conversation,
    can_transition,
)


@pytest.mark.parametrize(
    "a,b",
    [
        ("PENDING_ACCEPTANCE", "ACCEPTED"),
        ("PENDING_ACCEPTANCE", "REJECTED"),
        ("PENDING_ACCEPTANCE", "CANCELLED"),
        ("PENDING_ACCEPTANCE", "EXPIRED"),
        ("ACCEPTED", "PREPARING"),
        ("ACCEPTED", "READY"),
        ("ACCEPTED", "CANCELLED"),
        ("PREPARING", "READY"),
        ("READY", "COMPLETED"),
        ("ACCEPTED", "COMPLETED"),
        ("READY", "NO_SHOW"),
        ("ACCEPTED", "NO_SHOW"),
        ("NO_SHOW", "COMPLETED"),
    ],
)
def test_allowed(a, b):
    assert can_transition(a, b)


@pytest.mark.parametrize(
    "a,b",
    [("PENDING_ACCEPTANCE", "READY"), ("REJECTED", "ACCEPTED"), ("COMPLETED", "READY"), ("READY", "CANCELLED"),
     ("PENDING_ACCEPTANCE", "NO_SHOW"), ("COMPLETED", "NO_SHOW")],
)
def test_forbidden(a, b):
    with pytest.raises(InvalidTransition):
        assert_transition(a, b)


def test_terminal_statuses():
    assert TERMINAL_STATUSES == {OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.EXPIRED, OrderStatus.COMPLETED,
                                 OrderStatus.NO_SHOW}


def test_conversation_transitions():
    assert can_change_conversation(ConversationState.IDLE, ConversationState.BUILDING_CART)
    assert can_change_conversation("AWAITING_CONFIRMATION", "BUILDING_CART")
    assert not can_change_conversation("IDLE", "AWAITING_CONFIRMATION")

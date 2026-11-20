"""Order status and conversation state machines (plan section 11).

Transitions are enforced here and in the order service with conditional UPDATEs, never by
the LLM. HUMAN_HANDOFF is a separate conversation flag (conversations.bot_enabled).
"""

from enum import StrEnum


class OrderStatus(StrEnum):
    PENDING_ACCEPTANCE = "PENDING_ACCEPTANCE"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    PREPARING = "PREPARING"
    READY = "READY"
    COMPLETED = "COMPLETED"


ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING_ACCEPTANCE: frozenset(
        {OrderStatus.ACCEPTED, OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.EXPIRED}
    ),
    # Owner may skip PREPARING and go straight to READY ("READY 1042" by SMS).
    OrderStatus.ACCEPTED: frozenset({OrderStatus.PREPARING, OrderStatus.READY, OrderStatus.CANCELLED}),
    OrderStatus.PREPARING: frozenset({OrderStatus.READY, OrderStatus.CANCELLED}),
    OrderStatus.READY: frozenset({OrderStatus.COMPLETED}),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
    OrderStatus.COMPLETED: frozenset(),
}

TERMINAL_STATUSES = frozenset(s for s, nxt in ORDER_TRANSITIONS.items() if not nxt)


class InvalidTransition(Exception):
    def __init__(self, current: str, target: str):
        super().__init__(f"cannot move order from {current} to {target}")
        self.current = current
        self.target = target


def can_transition(current: OrderStatus | str, target: OrderStatus | str) -> bool:
    return OrderStatus(target) in ORDER_TRANSITIONS[OrderStatus(current)]


def assert_transition(current: OrderStatus | str, target: OrderStatus | str) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(str(current), str(target))


class ConversationState(StrEnum):
    IDLE = "IDLE"
    BUILDING_CART = "BUILDING_CART"
    COLLECTING_DETAILS = "COLLECTING_DETAILS"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"


CONVERSATION_TRANSITIONS: dict[ConversationState, frozenset[ConversationState]] = {
    ConversationState.IDLE: frozenset({ConversationState.BUILDING_CART}),
    ConversationState.BUILDING_CART: frozenset(
        {ConversationState.COLLECTING_DETAILS, ConversationState.AWAITING_CONFIRMATION, ConversationState.IDLE}
    ),
    ConversationState.COLLECTING_DETAILS: frozenset(
        {ConversationState.BUILDING_CART, ConversationState.AWAITING_CONFIRMATION, ConversationState.IDLE}
    ),
    # Confirmation -> IDLE once the order is submitted (the order then has its own status).
    ConversationState.AWAITING_CONFIRMATION: frozenset(
        {ConversationState.BUILDING_CART, ConversationState.COLLECTING_DETAILS, ConversationState.IDLE}
    ),
}


def can_change_conversation(current: ConversationState | str, target: ConversationState | str) -> bool:
    current, target = ConversationState(current), ConversationState(target)
    return current == target or target in CONVERSATION_TRANSITIONS[current]

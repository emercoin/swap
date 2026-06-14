"""Order state machine — the allowed transitions of §5.

Centralised so every status change goes through `assert_transition`; an illegal
move (e.g. delivering EMC twice) raises instead of silently corrupting an order.
"""
from __future__ import annotations

from .models import OrderStatus

# status → set of statuses it may move to
TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.CREATED: {OrderStatus.AWAITING_PAYMENT, OrderStatus.EXPIRED},
    OrderStatus.AWAITING_PAYMENT: {
        OrderStatus.CONFIRMED,
        OrderStatus.UNDERPAID,
        OrderStatus.OVERPAID,
        OrderStatus.AML_HOLD,
        OrderStatus.EXPIRED,
    },
    # underpaid/overpaid can be resolved back to confirmed (top-up / accept) or end
    OrderStatus.UNDERPAID: {OrderStatus.CONFIRMED, OrderStatus.EXPIRED},
    OrderStatus.OVERPAID: {OrderStatus.CONFIRMED},
    OrderStatus.AML_HOLD: {OrderStatus.CONFIRMED, OrderStatus.EXPIRED},
    OrderStatus.CONFIRMED: {OrderStatus.EMC_DELIVERED, OrderStatus.DELIVER_FAILED},
    OrderStatus.DELIVER_FAILED: {OrderStatus.EMC_DELIVERED, OrderStatus.CONFIRMED},
    OrderStatus.EMC_DELIVERED: {OrderStatus.NOTIFIED},
    OrderStatus.NOTIFIED: set(),
    OrderStatus.EXPIRED: set(),
}

TERMINAL: frozenset[OrderStatus] = frozenset(
    {OrderStatus.NOTIFIED, OrderStatus.EXPIRED}
)


def can_transition(src: OrderStatus, dst: OrderStatus) -> bool:
    return dst in TRANSITIONS.get(src, set())


class IllegalTransition(Exception):
    def __init__(self, src: OrderStatus, dst: OrderStatus) -> None:
        super().__init__(f"illegal order transition {src} → {dst}")
        self.src, self.dst = src, dst


def assert_transition(src: OrderStatus, dst: OrderStatus) -> None:
    if not can_transition(src, dst):
        raise IllegalTransition(src, dst)

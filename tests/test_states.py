from swap.models import OrderStatus
from swap.states import IllegalTransition, assert_transition, can_transition

import pytest


def test_happy_path_transitions():
    chain = [
        OrderStatus.CREATED,
        OrderStatus.AWAITING_PAYMENT,
        OrderStatus.CONFIRMED,
        OrderStatus.EMC_DELIVERED,
        OrderStatus.NOTIFIED,
    ]
    for src, dst in zip(chain, chain[1:]):
        assert can_transition(src, dst)
        assert_transition(src, dst)  # does not raise


def test_terminal_states_are_dead_ends():
    for terminal in (OrderStatus.NOTIFIED, OrderStatus.EXPIRED):
        for dst in OrderStatus:
            assert not can_transition(terminal, dst)


def test_no_double_delivery():
    # once delivered, you can only notify — never re-confirm/re-deliver
    assert not can_transition(OrderStatus.EMC_DELIVERED, OrderStatus.CONFIRMED)
    with pytest.raises(IllegalTransition):
        assert_transition(OrderStatus.EMC_DELIVERED, OrderStatus.EMC_DELIVERED)


def test_deliver_failed_can_retry():
    assert can_transition(OrderStatus.DELIVER_FAILED, OrderStatus.EMC_DELIVERED)

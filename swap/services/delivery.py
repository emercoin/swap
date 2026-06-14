"""Deliver EMC from the reserve to the order's destination — idempotent.

Called once an order is `confirmed`. Idempotency is critical: one order = one
EMC delivery, never double-pay. We guard on the order's current status (only
`confirmed`/`deliver_failed` may deliver) and record the emc_txid before moving
to `emc_delivered`.
"""
from __future__ import annotations

import logging

import httpx

from .. import repository
from ..clients.adapter import AdapterClient, AdapterError
from ..models import OrderStatus

log = logging.getLogger("swap.delivery")


async def deliver(order_id: int, adapter: AdapterClient) -> str:
    """Send EMC to the order's destination. Returns the emc_txid.

    Safe to retry: if the order already carries an emc_txid we return it without
    sending again.
    """
    order = await repository.get_order(order_id)
    if order is None:
        raise LookupError(f"order {order_id} not found")

    if order["emc_txid"]:
        return order["emc_txid"]  # already delivered — idempotent no-op

    status = OrderStatus(order["status"])
    if status not in (OrderStatus.CONFIRMED, OrderStatus.DELIVER_FAILED):
        raise RuntimeError(f"order {order_id} not deliverable in status {status}")

    try:
        txid = await adapter.send_emc(
            address=order["destination_emc"],
            amount=order["emc_amount"],
            comment=f"swap order {order_id}",
        )
    except (AdapterError, httpx.HTTPError) as exc:
        # Covers both adapter-level rejections (4xx/5xx) and the adapter being
        # unreachable (connect/timeout) — either way the delivery did not happen.
        log.error("EMC delivery failed for order %s: %s", order_id, exc)
        if status == OrderStatus.CONFIRMED:
            await repository.update_status(order_id, OrderStatus.DELIVER_FAILED)
        raise

    await repository.set_emc_txid(order_id, txid)
    await repository.update_status(order_id, OrderStatus.EMC_DELIVERED)
    log.info("order %s delivered %s EMC, txid=%s", order_id, order["emc_amount"], txid)
    return txid

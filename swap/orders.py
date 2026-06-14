"""buy_emc business logic — shared by the REST API and the MCP tool.

Keeps validation, idempotency and deposit-address derivation in one place so the
two front doors (REST, MCP) cannot drift apart.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import repository
from .config import settings
from .models import BuyEmcResponse, OrderStatus
from .tron.hd import derive_deposit_address


class OrderError(Exception):
    """Caller-facing validation error (maps to HTTP 400 / MCP error)."""


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


async def buy_emc(
    *,
    service_id: int,
    amount_usdt: float,
    destination_emc_address: str,
    callback_url: str,
    ref: str,
) -> BuyEmcResponse:
    """Create (or return the existing, idempotent) order and its deposit address."""
    if amount_usdt < settings.min_usdt:
        raise OrderError(f"amount below minimum {settings.min_usdt} USDT")
    if amount_usdt > settings.max_usdt:
        raise OrderError(f"amount above cap {settings.max_usdt} USDT")

    # Idempotency: (service_id, ref) → one order, one EMC delivery.
    existing = await repository.find_order_by_ref(service_id, ref)
    if existing is not None:
        return _to_response(existing)

    emc_amount = round(amount_usdt * settings.emc_per_usdt, 8)
    expires_at = _iso(datetime.now(timezone.utc) + timedelta(minutes=settings.order_ttl_minutes))

    order_id = await repository.insert_order(
        service_id=service_id,
        ref=ref,
        amount_usdt=amount_usdt,
        emc_amount=emc_amount,
        destination_emc=destination_emc_address,
        callback_url=callback_url,
        expires_at=expires_at,
    )

    # HD address index = order_id → globally unique, collision-free deposit address.
    deposit_address = derive_deposit_address(order_id)
    await repository.set_deposit_address(order_id, deposit_address)

    return BuyEmcResponse(
        order_id=order_id,
        deposit_address=deposit_address,
        amount_usdt=amount_usdt,
        emc_amount=emc_amount,
        status=OrderStatus.AWAITING_PAYMENT,
        expires_at=expires_at,
    )


def _to_response(row) -> BuyEmcResponse:
    return BuyEmcResponse(
        order_id=row["id"],
        deposit_address=row["deposit_address"],
        amount_usdt=row["amount_usdt"],
        emc_amount=row["emc_amount"],
        status=OrderStatus(row["status"]),
        expires_at=row["expires_at"],
    )

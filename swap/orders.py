"""buy_emc business logic — shared by the REST API and the MCP tool.

Payments land on one shared deposit address and are matched by a unique
**amount tag**: the caller's nominal amount nudged up by the smallest number of
micro-USDT that makes the exact pay amount globally unique. The buyer must pay
that exact figure (per the terms, imprecise amounts are not tracked/refunded).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx

from . import repository
from .clients.adapter import AdapterClient, AdapterError
from .config import settings, validate_amount_usdt
from .models import BuyEmcResponse, OrderStatus

log = logging.getLogger("swap.orders")


class OrderError(Exception):
    """Caller-facing validation error (maps to HTTP 400 / MCP error)."""


class ReserveError(Exception):
    """EMC reserve can't cover this order — service temporarily unavailable (503)."""


class CapacityError(Exception):
    """Too many orders awaiting payment right now — temporary back-pressure (503)."""


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


async def buy_emc(
    *,
    service_id: int,
    amount_usdt: float,
    destination_emc_address: str,
    callback_url: str,
    ref: str,
    adapter: AdapterClient | None = None,
) -> BuyEmcResponse:
    """Create (or return the existing, idempotent) order with a unique pay amount."""
    # Boundary guard (finite + one of the fixed denominations). The input surfaces
    # validate too, but re-check here so a direct/internal call can't smuggle a bad
    # amount into the tag-allocation algorithm below.
    try:
        validate_amount_usdt(amount_usdt)
    except ValueError as exc:
        raise OrderError(str(exc))
    if not settings.deposit_address:
        raise OrderError("deposit address not configured")

    # Idempotency: (service_id, ref) → one order, one EMC delivery.
    existing = await repository.find_order_by_ref(service_id, ref)
    if existing is not None:
        return _to_response(existing)

    # Back-pressure: cap concurrent awaiting orders so order-creation spam can't
    # exhaust the amount-tag space / DB rows / the expiry sweep. Checked after
    # idempotency (a repeat ref always resolves) and before the reserve call.
    if settings.max_awaiting_orders and (
        await repository.count_awaiting() >= settings.max_awaiting_orders
    ):
        raise CapacityError("too many pending orders; try again shortly")

    # Reserve pre-flight: never take USDT we can't deliver EMC for (out-of-service
    # when the hot wallet, minus outstanding promises, can't cover this order).
    await _check_reserve(round(amount_usdt * settings.emc_per_usdt, 8), adapter)

    base_units = round(amount_usdt * 1_000_000)
    expires_at = _iso(datetime.now(timezone.utc) + timedelta(minutes=settings.order_ttl_minutes))

    # Probe amount tags upward from the requested figure, taking the first one not
    # held by an ACTIVE order (idx_orders_active_amount). Terminal orders release
    # their tag, so this reuses the smallest free figure rather than creeping up.
    for k in range(settings.tag_max_tries):
        pay_units = base_units + k * settings.tag_step_units
        pay_amount = round(pay_units / 1_000_000, 6)
        emc_amount = round(pay_amount * settings.emc_per_usdt, 8)
        try:
            order_id = await repository.insert_order(
                service_id=service_id,
                ref=ref,
                amount_usdt=pay_amount,
                emc_amount=emc_amount,
                destination_emc=destination_emc_address,
                callback_url=callback_url,
                expires_at=expires_at,
            )
        except sqlite3.IntegrityError:
            # Either (service, ref) raced in, or this amount tag is taken.
            dup = await repository.find_order_by_ref(service_id, ref)
            if dup is not None:
                return _to_response(dup)
            continue  # amount-tag collision → try the next tag
        return BuyEmcResponse(
            order_id=order_id,
            deposit_address=settings.deposit_address,
            amount_usdt=pay_amount,
            emc_amount=emc_amount,
            status=OrderStatus.AWAITING_PAYMENT,
            expires_at=expires_at,
        )

    raise OrderError("could not allocate a unique payment amount; try again")


async def _check_reserve(emc_amount: float, adapter: AdapterClient | None) -> None:
    """Raise ReserveError unless the hot wallet, net of outstanding promises and a
    buffer, can cover `emc_amount`. Inability to verify (adapter down) also fails
    closed — we won't take money we can't honour."""
    if not settings.emc_reserve_check:
        return
    created = adapter is None
    if created:
        adapter = AdapterClient()
    try:
        balance = float((await adapter.balance())["balance"])
    except (AdapterError, httpx.HTTPError) as exc:
        log.warning("reserve check failed (adapter unavailable): %s", exc)
        raise ReserveError("cannot verify EMC reserve; service temporarily unavailable")
    finally:
        if created:
            await adapter.aclose()

    available = balance - await repository.outstanding_emc() - settings.emc_reserve_buffer
    if available < emc_amount:
        log.warning("EMC reserve too low: need %.8f, available %.8f", emc_amount, available)
        raise ReserveError("insufficient EMC reserve; service temporarily unavailable")


def _to_response(row) -> BuyEmcResponse:
    return BuyEmcResponse(
        order_id=row["id"],
        deposit_address=settings.deposit_address,
        amount_usdt=row["amount_usdt"],
        emc_amount=row["emc_amount"],
        status=OrderStatus(row["status"]),
        expires_at=row["expires_at"],
    )

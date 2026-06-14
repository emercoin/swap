"""Background watcher — the order pipeline.

One periodic loop drives every awaiting order through:

    awaiting_payment → (deposit seen) → confirmations → AML screen
        → confirmed → deliver EMC → emc_delivered → signed callback → notified

plus the off-happy-path branches (underpaid / overpaid / aml_hold / expired).
Each stage is idempotent and persisted, so a crash mid-pipeline resumes safely.

This orchestration is scaffolded: the TRON-facing reads (TronGridClient) are
NotImplemented until verified in the sandbox (§7.3). Delivery, callback signing
and the state machine below are real.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from .. import repository
from ..clients.adapter import AdapterClient
from ..clients.trongrid import TronGridClient
from ..config import settings
from ..models import OrderStatus
from . import aml, callback, delivery

log = logging.getLogger("swap.watcher")

POLL_INTERVAL_SECONDS = 15

# USDT has 6 decimals; tolerate sub-micro float noise when matching amounts.
_PAYMENT_EPSILON = 1e-6


async def run(stop: asyncio.Event) -> None:
    """Main loop; cancel by setting `stop`."""
    adapter = AdapterClient()
    tron = TronGridClient()
    log.info("watcher started (poll=%ss, confirmations=%s)",
             POLL_INTERVAL_SECONDS, settings.confirmations_required)
    last_aml_refresh = 0.0
    try:
        while not stop.is_set():
            try:
                if time.monotonic() - last_aml_refresh >= settings.aml_refresh_hours * 3600:
                    await aml.load_blacklists()          # initial load + periodic refresh
                    last_aml_refresh = time.monotonic()
            except Exception:
                log.exception("AML: blacklist load failed (screening continues with current list)")
            try:
                await tick(adapter, tron)
            except Exception:  # never let one bad tick kill the loop
                log.exception("watcher tick failed")
            await _sleep_or_stop(stop, POLL_INTERVAL_SECONDS)
    finally:
        await adapter.aclose()
        await tron.aclose()


async def tick(adapter: AdapterClient, tron: TronGridClient) -> None:
    await _expire_stale()
    await _scan_payments(tron)          # awaiting_payment → confirmed / underpaid / overpaid / aml_hold
    await _deliver_confirmed(adapter)   # confirmed → emc_delivered
    await _notify_delivered()           # emc_delivered → notified


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_plus(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


async def _expire_stale() -> None:
    now = _now_iso()
    for row in await repository.list_orders_by_status(OrderStatus.AWAITING_PAYMENT):
        if row["expires_at"] < now:
            await repository.update_status(row["id"], OrderStatus.EXPIRED)
            log.info("order %s expired", row["id"])


async def _scan_payments(tron: TronGridClient) -> None:
    """Detect final USDT on each awaiting deposit address → AML → amount-check.

    Every transfer TronGrid returns here is already irreversible (only_confirmed),
    so finality needs no extra step. Order of checks matters: AML first, so a
    blacklisted sender can never reach `confirmed`/delivery even with an exact
    amount (§3 — protects working capital from a freeze).
    """
    for order in await repository.list_orders_by_status(OrderStatus.AWAITING_PAYMENT):
        address = order["deposit_address"]
        if not address:
            continue
        try:
            transfers = await tron.usdt_transfers_to(address)
        except Exception:
            log.exception("trongrid scan failed for order %s", order["id"])
            continue
        if not transfers:
            continue

        # Persist every seen (final) transfer — idempotent on txid.
        for t in transfers:
            await repository.record_deposit(
                order_id=order["id"], tron_txid=t.txid, from_address=t.from_address,
                amount_usdt=t.amount_usdt, confirmations=settings.confirmations_required,
            )

        # AML screen of every distinct sender; any hit → hold, deliver nothing.
        senders = {t.from_address for t in transfers}
        blacklisted = None
        for sender in senders:
            res = await aml.screen_full(sender, tron)
            await repository.record_aml_check(
                order_id=order["id"], address=sender,
                result="clear" if res.clear else "hit", source=res.source,
            )
            if not res.clear:
                blacklisted = sender
        if blacklisted is not None:
            await repository.update_status(order["id"], OrderStatus.AML_HOLD)
            log.warning("order %s AML hold: sender %s", order["id"], blacklisted)
            continue

        total = round(sum(t.amount_usdt for t in transfers), 6)
        expected = order["amount_usdt"]
        if total + _PAYMENT_EPSILON < expected:
            await repository.update_status(order["id"], OrderStatus.UNDERPAID)
            log.info("order %s underpaid: %.6f < %.6f", order["id"], total, expected)
        elif total > expected + _PAYMENT_EPSILON:
            await repository.update_status(order["id"], OrderStatus.OVERPAID)
            log.info("order %s overpaid: %.6f > %.6f", order["id"], total, expected)
        else:
            await repository.update_status(order["id"], OrderStatus.CONFIRMED)
            log.info("order %s confirmed: %.6f USDT", order["id"], total)


async def _deliver_confirmed(adapter: AdapterClient) -> None:
    for row in await repository.list_orders_by_status(OrderStatus.CONFIRMED):
        try:
            await delivery.deliver(row["id"], adapter)
        except Exception:
            log.exception("delivery failed for order %s", row["id"])


async def _notify_delivered() -> None:
    """Send the signed callback for each delivered order; retry with backoff.

    An order sits in `emc_delivered` exactly until its callback is acknowledged
    (2xx) → then `notified`. So this stage owns the whole retry lifecycle:
    enqueue once, resend when due, give up after `callback_max_retries` (the
    service then falls back to polling GET /order/{id}). EMC is already out, so
    a never-acknowledged callback never loses funds — only a notification.
    """
    now = _now_iso()
    for order in await repository.list_orders_by_status(OrderStatus.EMC_DELIVERED):
        cb = await repository.get_callback_for_order(order["id"])
        if cb is None:
            await repository.enqueue_callback(
                order_id=order["id"],
                url=order["callback_url"],
                payload={
                    "ref": order["ref"],
                    "order_id": order["id"],
                    "status": callback.PAID,
                    "emc_txid": order["emc_txid"],
                },
            )
            cb = await repository.get_callback_for_order(order["id"])

        if cb["delivered"]:
            # Edge case: callback acked but the status bump didn't land — fix it.
            await repository.update_status(order["id"], OrderStatus.NOTIFIED)
            continue
        if cb["attempts"] >= settings.callback_max_retries:
            continue  # exhausted — leave delivered; service polls for status
        if cb["next_retry_at"] and cb["next_retry_at"] > now:
            continue  # backing off; not due yet

        svc = await repository.get_service_by_id(order["service_id"])
        if svc is None:  # service deleted mid-flight; nothing to sign with
            log.error("order %s: service %s missing, cannot sign callback",
                      order["id"], order["service_id"])
            continue

        delivered, code = await callback.send_for_order(
            order_row=order, secret=svc["callback_secret"]
        )
        if delivered:
            await repository.mark_callback_delivered(cb["id"], code)
            await repository.update_status(order["id"], OrderStatus.NOTIFIED)
            log.info("order %s notified (callback %s)", order["id"], code)
        else:
            next_attempt = cb["attempts"] + 1
            next_retry_at = _now_plus(callback.backoff_seconds(next_attempt))
            await repository.record_callback_failure(
                cb["id"], last_status=code, next_retry_at=next_retry_at
            )
            log.warning("order %s callback failed (%s), retry #%s at %s",
                        order["id"], code, next_attempt, next_retry_at)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass

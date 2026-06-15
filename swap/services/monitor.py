"""Periodic EMC reserve monitor — proactive balance log + low-reserve alert.

The reserve pre-flight in `orders.py` is *reactive*: it refuses an order only once
the hot wallet can't cover it, i.e. a buyer is the one who discovers the shortfall
(a 503). This watches the same number proactively: on a cadence it logs the
hot-wallet EMC balance and outstanding obligations, and emits a WARNING while the
available headroom (balance − obligations − buffer) is below a watermark — so the
operator tops up the reserve *before* customers see out-of-service.

Headroom is computed exactly like the pre-flight (`repository.outstanding_emc`
counts paid, undelivered orders), so the alert fires for the same condition the
pre-flight will start rejecting on. Driven from the watcher loop on its own timer;
the periodic interval throttles the WARNING, so it can't spam the per-tick loop.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from .. import repository
from ..clients.adapter import AdapterClient, AdapterError
from ..config import settings

log = logging.getLogger("swap.monitor")


@dataclass
class ReserveStatus:
    balance: float          # hot-wallet EMC reported by the adapter
    outstanding: float      # EMC owed by paid, undelivered orders
    buffer: float           # extra EMC kept in hand (emc_reserve_buffer)
    available: float        # balance − outstanding − buffer
    watermark: float        # low-reserve threshold
    low: bool               # available < watermark


async def read_reserve(adapter: AdapterClient) -> ReserveStatus:
    """Snapshot the EMC reserve headroom (raises on an unreachable adapter)."""
    balance = float((await adapter.balance())["balance"])
    outstanding = await repository.outstanding_emc()
    buffer = settings.emc_reserve_buffer
    available = balance - outstanding - buffer
    watermark = settings.reserve_low_watermark
    return ReserveStatus(
        balance=balance, outstanding=outstanding, buffer=buffer,
        available=available, watermark=watermark, low=available < watermark,
    )


class ReserveMonitor:
    """Stateful, self-throttling reserve check for the watcher loop.

    Call `run_if_due` every tick; it actually queries the adapter only once per
    `reserve_monitor_interval_minutes`. While low it WARNs on each due check (the
    interval, not the tick, bounds the rate); it logs a one-off recovery notice when
    the headroom climbs back over the watermark."""

    def __init__(self) -> None:
        self._next_due = 0.0     # monotonic; 0 → first check runs immediately
        self._was_low = False    # to log recovery exactly once

    async def run_if_due(self, adapter: AdapterClient) -> ReserveStatus | None:
        """Check + log/alert if the interval has elapsed, else no-op. Returns the
        status when a check ran (mostly for tests), None otherwise."""
        now = time.monotonic()
        if now < self._next_due:
            return None
        self._next_due = now + settings.reserve_monitor_interval_minutes * 60
        try:
            return await self.check(adapter)
        except (AdapterError, httpx.HTTPError) as exc:
            log.warning("reserve monitor: balance unavailable (adapter down?): %s", exc)
            return None

    async def check(self, adapter: AdapterClient) -> ReserveStatus:
        """Run one check, logging the balance summary and the low/recovery alert."""
        s = await read_reserve(adapter)
        log.info(
            "EMC reserve: balance=%.8f outstanding=%.8f buffer=%.8f available=%.8f "
            "(watermark %.8f)", s.balance, s.outstanding, s.buffer, s.available, s.watermark,
        )
        if s.low:
            log.warning(
                "LOW EMC RESERVE: available %.8f < watermark %.8f — top up the hot "
                "wallet or new orders will be refused (503)", s.available, s.watermark,
            )
        elif self._was_low:
            log.info("EMC reserve recovered: available %.8f ≥ watermark %.8f",
                     s.available, s.watermark)
        self._was_low = s.low
        return s

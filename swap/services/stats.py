"""Public stats digest — the data behind /stats.html and GET /web/stats.

A keyless, public proof-of-reserves view: the EMC hot-wallet reserve, the USDT
balance on the shared deposit address, lifetime order counts, and 24h/7d activity.
Because the endpoint is public it must not hit the adapter / TronGrid on every page
load, so a short module-level TTL cache (`stats_cache_ttl_seconds`) fronts the two
upstream reads. Upstream failures degrade to `None` for that one balance — the DB
metrics always render — so a flaky adapter or a TronGrid 429 never 500s the page.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from .. import repository
from ..clients.adapter import AdapterClient, AdapterError
from ..clients.trongrid import TronGridClient
from ..config import settings
from .monitor import read_reserve

log = logging.getLogger("swap.stats")

# (monotonic_expiry, snapshot) — None until the first build. Module-level so it is
# shared across requests in the single app process.
_cache: tuple[float, dict] | None = None

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"   # matches schema.sql created_at / updated_at


async def _emc_reserve() -> dict | None:
    """EMC reserve headroom from the adapter, or None if the adapter is unreachable."""
    adapter = AdapterClient()
    try:
        s = await read_reserve(adapter)
    except (AdapterError, httpx.HTTPError) as exc:
        log.warning("stats: EMC reserve unavailable (adapter down?): %s", exc)
        return None
    finally:
        await adapter.aclose()
    return {
        "balance": s.balance, "outstanding": s.outstanding, "buffer": s.buffer,
        "available": s.available, "watermark": s.watermark, "low": s.low,
    }


async def _usdt_deposit_balance() -> float | None:
    """USDT on the shared deposit address, or None if unset / TronGrid is unreachable."""
    if not settings.deposit_address:
        return None
    tron = TronGridClient()
    try:
        return await tron.usdt_balance_of(settings.deposit_address)
    except (httpx.HTTPError, RuntimeError) as exc:
        log.warning("stats: USDT balance unavailable (TronGrid down?): %s", exc)
        return None
    finally:
        await tron.aclose()


async def _build() -> dict:
    """Assemble a fresh snapshot (the uncached path)."""
    now = datetime.now(timezone.utc)
    counts = await repository.order_counts()
    snapshot = {
        "generated_at": now.strftime(_TS_FMT),
        "balances": {
            "deposit_address": settings.deposit_address,
            "usdt_deposit": await _usdt_deposit_balance(),
            "emc_reserve": await _emc_reserve(),
        },
        "orders": {
            "total": counts.get("total", 0),
            # "delivered" = EMC sent (emc_delivered) and/or fully settled (notified).
            "delivered": counts.get("emc_delivered", 0) + counts.get("notified", 0),
            "delivered_emc": await repository.delivered_emc_total(),
            "by_status": {k: v for k, v in counts.items() if k != "total"},
        },
        "activity": {
            "last_24h": await repository.activity_since((now - timedelta(days=1)).strftime(_TS_FMT)),
            "last_7d": await repository.activity_since((now - timedelta(days=7)).strftime(_TS_FMT)),
        },
    }
    return snapshot


async def get_stats() -> dict:
    """Return the digest snapshot, served from the TTL cache when fresh."""
    global _cache
    now = time.monotonic()
    if _cache is not None and now < _cache[0]:
        return _cache[1]
    snapshot = await _build()
    _cache = (now + settings.stats_cache_ttl_seconds, snapshot)
    return snapshot


def reset_cache() -> None:
    """Drop the cached snapshot (tests; or a forced refresh)."""
    global _cache
    _cache = None

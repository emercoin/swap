"""DB access layer — the only module that writes SQL.

Status changes go through `update_status`, which enforces the state machine
(states.assert_transition) inside the same connection so an illegal move can
never be persisted.
"""
from __future__ import annotations

import json

import aiosqlite

from .db import get_conn
from .models import OrderStatus
from .states import assert_transition


# --- services --------------------------------------------------------------

async def create_service(name: str, api_key: str, callback_secret: str) -> int:
    conn = await get_conn()
    cur = await conn.execute(
        "INSERT INTO services (name, api_key, callback_secret) VALUES (?, ?, ?)",
        (name, api_key, callback_secret),
    )
    await conn.commit()
    return cur.lastrowid


async def get_service_by_api_key(api_key: str) -> aiosqlite.Row | None:
    conn = await get_conn()
    cur = await conn.execute("SELECT * FROM services WHERE api_key = ?", (api_key,))
    return await cur.fetchone()


async def get_service_by_id(service_id: int) -> aiosqlite.Row | None:
    conn = await get_conn()
    cur = await conn.execute("SELECT * FROM services WHERE id = ?", (service_id,))
    return await cur.fetchone()


async def get_service_by_name(name: str) -> aiosqlite.Row | None:
    conn = await get_conn()
    cur = await conn.execute("SELECT * FROM services WHERE name = ?", (name,))
    return await cur.fetchone()


# --- orders ----------------------------------------------------------------

async def find_order_by_ref(service_id: int, ref: str) -> aiosqlite.Row | None:
    conn = await get_conn()
    cur = await conn.execute(
        "SELECT * FROM orders WHERE service_id = ? AND ref = ?", (service_id, ref)
    )
    return await cur.fetchone()


async def insert_order(
    *,
    service_id: int,
    ref: str,
    amount_usdt: float,
    emc_amount: float,
    destination_emc: str,
    callback_url: str,
    expires_at: str,
) -> int:
    """Insert an `awaiting_payment` order. `amount_usdt` is the unique tagged pay
    amount — a UNIQUE conflict here means either a duplicate (service, ref) or an
    amount-tag collision; the caller (orders.buy_emc) disambiguates and retries."""
    conn = await get_conn()
    cur = await conn.execute(
        """INSERT INTO orders
               (service_id, ref, amount_usdt, emc_amount, destination_emc,
                callback_url, status, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            service_id, ref, amount_usdt, emc_amount, destination_emc,
            callback_url, OrderStatus.AWAITING_PAYMENT, expires_at,
        ),
    )
    await conn.commit()
    return cur.lastrowid


async def get_order(order_id: int) -> aiosqlite.Row | None:
    conn = await get_conn()
    cur = await conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    return await cur.fetchone()


async def find_open_order_by_amount(amount_usdt: float) -> aiosqlite.Row | None:
    """An awaiting order whose tagged amount exactly matches a payment (within
    half a micro-USDT). Amounts are globally unique → at most one match."""
    conn = await get_conn()
    cur = await conn.execute(
        """SELECT * FROM orders
            WHERE status = ? AND ABS(amount_usdt - ?) < 0.0000005""",
        (OrderStatus.AWAITING_PAYMENT, amount_usdt),
    )
    return await cur.fetchone()


async def list_orders_by_status(status: OrderStatus) -> list[aiosqlite.Row]:
    conn = await get_conn()
    cur = await conn.execute("SELECT * FROM orders WHERE status = ?", (status,))
    return list(await cur.fetchall())


async def update_status(order_id: int, new: OrderStatus) -> None:
    """Persist a status change, enforcing the state machine."""
    conn = await get_conn()
    cur = await conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,))
    row = await cur.fetchone()
    if row is None:
        raise LookupError(f"order {order_id} not found")
    assert_transition(OrderStatus(row["status"]), new)
    await conn.execute(
        """UPDATE orders SET status = ?,
              updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE id = ?""",
        (new, order_id),
    )
    await conn.commit()


async def outstanding_emc() -> float:
    """EMC we've promised but not yet delivered (sum over live, undelivered orders).
    Used to size the reserve pre-flight check so concurrent orders are accounted."""
    conn = await get_conn()
    cur = await conn.execute(
        "SELECT COALESCE(SUM(emc_amount), 0) AS s FROM orders "
        "WHERE emc_txid IS NULL AND status != ?",
        (OrderStatus.EXPIRED,),
    )
    row = await cur.fetchone()
    return float(row["s"] or 0)


async def increment_delivery_attempts(order_id: int) -> None:
    conn = await get_conn()
    await conn.execute(
        """UPDATE orders SET delivery_attempts = delivery_attempts + 1,
              updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE id = ?""",
        (order_id,),
    )
    await conn.commit()


async def set_emc_txid(order_id: int, txid: str) -> None:
    conn = await get_conn()
    await conn.execute(
        """UPDATE orders SET emc_txid = ?,
              updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE id = ?""",
        (txid, order_id),
    )
    await conn.commit()


# --- deposits / aml / sweeps / callbacks -----------------------------------

async def deposit_seen(tron_txid: str) -> bool:
    """Whether this transfer was already processed (skip re-processing per tick)."""
    conn = await get_conn()
    cur = await conn.execute("SELECT 1 FROM deposits WHERE tron_txid = ?", (tron_txid,))
    return await cur.fetchone() is not None


async def record_deposit(
    *, order_id: int | None, tron_txid: str, from_address: str,
    amount_usdt: float, confirmations: int,
) -> None:
    """Record a confirmed transfer. order_id is None for an unmatched payment."""
    conn = await get_conn()
    await conn.execute(
        """INSERT INTO deposits (order_id, tron_txid, from_address, amount_usdt, confirmations)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(tron_txid) DO NOTHING""",
        (order_id, tron_txid, from_address, amount_usdt, confirmations),
    )
    await conn.commit()


async def record_aml_check(
    *, order_id: int, address: str, result: str, source: str | None
) -> None:
    conn = await get_conn()
    await conn.execute(
        "INSERT INTO aml_checks (order_id, address, result, source) VALUES (?, ?, ?, ?)",
        (order_id, address, result, source),
    )
    await conn.commit()


async def enqueue_callback(*, order_id: int, url: str, payload: dict) -> int:
    conn = await get_conn()
    cur = await conn.execute(
        "INSERT INTO callbacks (order_id, url, payload) VALUES (?, ?, ?)",
        (order_id, url, json.dumps(payload, separators=(",", ":"), sort_keys=True)),
    )
    await conn.commit()
    return cur.lastrowid


async def get_callback_for_order(order_id: int) -> aiosqlite.Row | None:
    """The (single) callback row for an order, if one has been enqueued."""
    conn = await get_conn()
    cur = await conn.execute(
        "SELECT * FROM callbacks WHERE order_id = ? ORDER BY id DESC LIMIT 1", (order_id,)
    )
    return await cur.fetchone()


async def mark_callback_delivered(callback_id: int, last_status: int) -> None:
    conn = await get_conn()
    await conn.execute(
        """UPDATE callbacks
              SET delivered = 1, last_status = ?, attempts = attempts + 1,
                  next_retry_at = NULL
            WHERE id = ?""",
        (last_status, callback_id),
    )
    await conn.commit()


async def record_callback_failure(
    callback_id: int, *, last_status: int, next_retry_at: str | None
) -> None:
    conn = await get_conn()
    await conn.execute(
        """UPDATE callbacks
              SET attempts = attempts + 1, last_status = ?, next_retry_at = ?
            WHERE id = ?""",
        (last_status, next_retry_at, callback_id),
    )
    await conn.commit()

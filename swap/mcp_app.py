"""MCP surface for swap — so an agent with USDT pays programmatically.

Target audience is AI agents (§1b): an agent holding USDT calls `buy_emc`
directly, no human in the loop. Same two operations as REST, exposed as MCP
tools over a thin FastMCP server (mirrors the emercoin gateway's mcp pattern).

Auth: the calling service's API key is passed as a tool argument here for the
skeleton; production should carry it in the transport (header / OAuth) like the
gateway's remote MCP server rather than as a visible parameter.

Run standalone (stdio):  `python -m swap.mcp_app`
"""
from __future__ import annotations

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from . import db, repository
from .orders import OrderError, buy_emc as _buy_emc

mcp = FastMCP("swap")


async def _resolve_service_id(api_key: str) -> int:
    row = await repository.get_service_by_api_key(api_key)
    if row is None or not row["active"]:
        raise ValueError("invalid API key")
    return row["id"]


@mcp.tool()
async def buy_emc(
    api_key: Annotated[str, Field(description="calling service API key")],
    amount_usdt: Annotated[float, Field(gt=0, description="USDT to collect (≤ cap)")],
    destination_emc_address: Annotated[str, Field(description="where EMC is delivered")],
    callback_url: Annotated[str, Field(description="signed POST lands here when paid")],
    ref: Annotated[str, Field(description="caller's invoice id (idempotency key)")],
) -> dict:
    """Open a swap order: pay USDT to the returned TRON deposit address, swap
    delivers EMC (×10) to destination on confirmation and signs a callback."""
    await db.connect()
    service_id = await _resolve_service_id(api_key)
    try:
        resp = await _buy_emc(
            service_id=service_id,
            amount_usdt=amount_usdt,
            destination_emc_address=destination_emc_address,
            callback_url=callback_url,
            ref=ref,
        )
    except OrderError as exc:
        raise ValueError(str(exc))
    return resp.model_dump()


@mcp.tool()
async def order_status(
    api_key: Annotated[str, Field(description="calling service API key")],
    order_id: Annotated[int, Field(description="order id from buy_emc")],
) -> dict:
    """Current status of an order (fallback when the callback hasn't arrived)."""
    await db.connect()
    service_id = await _resolve_service_id(api_key)
    row = await repository.get_order(order_id)
    if row is None or row["service_id"] != service_id:
        raise ValueError("order not found")
    return {
        "order_id": row["id"],
        "ref": row["ref"],
        "status": row["status"],
        "amount_usdt": row["amount_usdt"],
        "emc_amount": row["emc_amount"],
        "deposit_address": row["deposit_address"],
        "emc_txid": row["emc_txid"],
        "expires_at": row["expires_at"],
    }


if __name__ == "__main__":
    mcp.run()

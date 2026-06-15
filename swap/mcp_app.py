"""MCP exchanger surface — so an AI agent holding USDT buys EMC programmatically.

This mirrors the keyless public **web** channel (`swap/web.py`), not the keyed
service-to-service API: an agent wanting EMC at its own address needs no account,
no API key and no callback — exactly like a human on the web page. The agent calls
`buy_emc` (amount + its EMC address), gets a shared deposit address and an EXACT
amount to pay, then polls `order_status` by an opaque token until EMC is delivered.

Backed by the same first-party "web" service row and `orders.buy_emc`, so it
inherits the global awaiting cap and reserve pre-flight; per-IP rate/concurrency
caps are reused from the web channel via the request's client IP. There is no
browser-style proof-of-work here (the caller is a program, not a page) — the
global cap and per-IP limits carry the anti-spam load.

Auth: none, by design — this is a public on-ramp ("pay for a service"), same as
the web page; we don't make a buyer register to hand us USDT.

Transport: mounted into the FastAPI app as Streamable HTTP at `/mcp` (see
`swap/main.py`); also runnable standalone over stdio: `python -m swap.mcp_app`.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Annotated, Iterator

from fastapi import HTTPException
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from . import db, web
from .config import settings

# DNS-rebinding protection guards browser-driven localhost servers; here the edge
# (Caddy/Cloudflare) terminates and validates Host, and the audience is server-side
# agents, so it is off by default (toggle via SWAP_MCP_DNS_REBINDING_PROTECTION).
_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=settings.mcp_dns_rebinding_protection
)

mcp = FastMCP(
    "swap",
    instructions=(
        "Buy EMC with USDT (TRC20). Call swap_config for the limits, then buy_emc "
        "with the USDT amount and your EMC address; pay the EXACT returned amount to "
        "the deposit address (one-way, no refunds), then poll order_status by token."
    ),
    stateless_http=True,
    json_response=True,
    transport_security=_security,
)
# Mounted under /mcp in the FastAPI app, so the endpoint itself sits at the mount root.
mcp.settings.streamable_http_path = "/"


@contextmanager
def _as_tool_error() -> Iterator[None]:
    """Translate the web layer's HTTPExceptions into plain MCP tool errors."""
    try:
        yield
    except HTTPException as exc:
        raise ValueError(str(exc.detail)) from None


async def _ready() -> None:
    """Make sure the DB and the first-party web service exist (idempotent). The HTTP
    app does this at startup; this also covers the standalone stdio entrypoint."""
    await db.connect()
    if web._web_service is None:
        await web.ensure_web_service()


def _request(ctx: Context):
    """The underlying Starlette request (for the client IP), or None over stdio."""
    try:
        return ctx.request_context.request
    except (ValueError, AttributeError):
        return None


class _NoRequest:
    """Stand-in when there is no HTTP request (stdio): no IP, so per-IP caps no-op."""
    headers: dict = {}
    client = None


@mcp.tool()
async def swap_config() -> dict:
    """The current limits so you can pick a valid amount: min/max USDT per order and
    the fixed EMC-per-USDT rate (EMC delivered = amount_usdt × emc_per_usdt)."""
    return {
        "min_usdt": settings.min_usdt,
        "max_usdt": settings.max_usdt,
        "emc_per_usdt": settings.emc_per_usdt,
    }


@mcp.tool()
async def buy_emc(
    ctx: Context,
    amount_usdt: Annotated[float, Field(gt=0, description="USDT to pay (within min/max from swap_config)")],
    destination_emc_address: Annotated[str, Field(description="your EMC address to receive EMC")],
) -> dict:
    """Open an order to buy EMC with USDT. Returns a shared TRON deposit address and
    an EXACT `amount_usdt` to send (TRC20) — pay that figure precisely; it is your
    order's tag (a wrong amount cannot be matched and is not refunded). On confirmed
    payment, EMC (amount_usdt × rate) is delivered to your address. Keep the returned
    `token` and poll `order_status`. No key, no callback; one-way."""
    await _ready()
    with _as_tool_error():
        resp = await web.create_public_order(
            amount_usdt,
            destination_emc_address,
            _request(ctx) or _NoRequest(),
            enforce_pow=False,
        )
    return resp.model_dump(mode="json")


@mcp.tool()
async def order_status(
    token: Annotated[str, Field(description="the token returned by buy_emc")],
) -> dict:
    """Current status of your order (poll until `notified`/`emc_delivered`). Returns
    the status, the exact amount, the EMC amount and address, and the EMC txid once
    delivered."""
    await _ready()
    with _as_tool_error():
        resp = await web.web_order_status(token)
    return resp.model_dump(mode="json")


@mcp.tool()
async def cancel_order(
    token: Annotated[str, Field(description="the token returned by buy_emc")],
) -> dict:
    """Drop an unpaid order early (frees its slot ahead of expiry). Only works while
    it is still awaiting payment; once a payment is in flight it is too late. A
    payment sent after cancellation matches nothing and is not refunded."""
    await _ready()
    with _as_tool_error():
        resp = await web.web_cancel_order(token)
    return resp.model_dump(mode="json")


if __name__ == "__main__":
    mcp.run()

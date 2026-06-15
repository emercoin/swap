"""MCP exchanger surface — so an AI agent holding USDT buys EMC programmatically.

This mirrors the keyless public **web** channel (`swap/web.py`), not the keyed
service-to-service API: an agent wanting EMC at its own address needs no account,
no API key and no callback — exactly like a human on the web page. The agent calls
`buy_emc` (amount + its EMC address), gets a shared deposit address and an EXACT
amount to pay, then polls `get_order_status` by an opaque token until EMC arrives.

Backed by the same first-party "web" service row and `orders.buy_emc`, so it
inherits the global awaiting cap and reserve pre-flight; per-IP rate/concurrency
caps are reused from the web channel via the request's client IP. There is no
browser-style proof-of-work here (the caller is a program, not a page) — the
global cap and per-IP limits carry the anti-spam load.

Tool definitions follow TDQS (verb_noun names, when/when-not + named siblings in
each description, behavior hints in annotations, Pydantic return types for output
schemas), so the surface scores well for agent tool-selection.

Auth: none, by design — this is a public on-ramp ("pay for a service"), same as
the web page; we don't make a buyer register to hand us USDT.

Transport: mounted into the FastAPI app as Streamable HTTP at `/mcp` (see
`swap/main.py`); also runnable standalone over stdio: `python -m swap.mcp_app`.
"""
from __future__ import annotations

import inspect
from contextlib import contextmanager
from typing import Annotated, Iterator

from fastapi import HTTPException
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.routing import Route

from . import db, web
from .config import settings
from .web import WebConfigResponse, WebOrderResponse, WebStatusResponse

# DNS-rebinding protection guards browser-driven localhost servers; here the edge
# (Caddy/Cloudflare) terminates and validates Host, and the audience is server-side
# agents, so it is off by default (toggle via SWAP_MCP_DNS_REBINDING_PROTECTION).
_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=settings.mcp_dns_rebinding_protection
)

mcp = FastMCP(
    "swap",
    instructions=(
        "Buy EMC with USDT (TRC20), no account needed. Flow: get_swap_config for the "
        "limits/rate → buy_emc with the USDT amount and your EMC address → send the "
        "EXACT returned amount to the deposit address (one-way, no refunds) → poll "
        "get_order_status by token until 'notified'. cancel_order drops an unpaid order."
    ),
    stateless_http=True,
    json_response=True,
    transport_security=_security,
)
def streamable_routes() -> list[Route]:
    """Routes serving the MCP Streamable HTTP endpoint at exactly `/mcp` and `/mcp/`.

    Registered directly on the main app's router (not `app.mount("/mcp", …)`): a
    prefix mount makes the canonical no-trailing-slash URL `/mcp` 307-redirect to
    `/mcp/` (and, with the Starlette wrapper, 405) — and several MCP clients (incl.
    MCP Inspector) POST to `/mcp` without the slash and don't follow the redirect, so
    they fail to connect. Two explicit routes pointing at the same bare ASGI handler
    make both forms return 200. The session manager is created here (lazily via
    `streamable_http_app`) and driven from the app lifespan."""
    mcp.streamable_http_app()                      # lazily build the session manager
    asgi = StreamableHTTPASGIApp(mcp.session_manager)
    return [Route(path, endpoint=asgi) for path in ("/mcp", "/mcp/")]


def tool(**kwargs):
    """Like `mcp.tool`, but derive the tool description from the function's docstring
    run through `inspect.cleandoc` — so the published description has no leading
    docstring indentation (cleaner for clients / TDQS) while the docstring stays the
    single source of truth in the source."""
    def decorate(fn):
        if "description" not in kwargs and fn.__doc__:
            kwargs["description"] = inspect.cleandoc(fn.__doc__)
        return mcp.tool(**kwargs)(fn)
    return decorate


# Param descriptions reused across tools (the token is the only handle to an order).
_TOKEN = Annotated[
    str,
    Field(description="opaque order handle returned by buy_emc; pass it back unchanged"),
]


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


@tool(
    title="Get EMC swap limits and rate",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def get_swap_config() -> WebConfigResponse:
    """Return the current order limits and fixed rate for buying EMC with USDT:
    min/max USDT per order and emc_per_usdt (EMC you receive = amount_usdt ×
    emc_per_usdt). Call this first to choose a valid amount for buy_emc. Read-only —
    it neither creates nor changes an order."""
    return WebConfigResponse(
        min_usdt=settings.min_usdt,
        max_usdt=settings.max_usdt,
        emc_per_usdt=settings.emc_per_usdt,
    )


@tool(
    title="Buy EMC with USDT (open an order)",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def buy_emc(
    ctx: Context,
    amount_usdt: Annotated[
        float, Field(gt=0, description="USDT to pay; must be within min/max from get_swap_config")
    ],
    destination_emc_address: Annotated[
        str, Field(description="your EMC address to receive EMC (legacy 'E…' or bech32 'em1…')")
    ],
    idempotency_key: Annotated[
        str,
        Field(default="", description="optional: a stable string you choose; retrying buy_emc "
              "with the same key + address + amount returns the SAME order instead of opening a "
              "new one (use it so a retry after a timeout doesn't create a duplicate)"),
    ] = "",
) -> WebOrderResponse:
    """Open an order to buy EMC with USDT (TRC20) and get back a shared TRON
    `deposit_address` plus the EXACT `amount_usdt` to send. This does NOT move funds:
    you then transfer that exact figure (it is your order's matching tag) to the
    deposit address; on confirmed payment, EMC (amount_usdt × rate) is delivered to
    your address automatically. Keep the returned `token` and poll get_order_status
    until 'notified'; to abandon before paying, call cancel_order. One-way — a wrong
    amount cannot be matched and is NOT refunded. By default each call opens a NEW
    order; pass a stable `idempotency_key` to make retries return the same order. Use
    get_swap_config first to pick a valid amount."""
    await _ready()
    with _as_tool_error():
        return await web.create_public_order(
            amount_usdt,
            destination_emc_address,
            _request(ctx) or _NoRequest(),
            enforce_pow=False,
            idempotency_key=idempotency_key,
        )


@tool(
    title="Get EMC order status",
    annotations={"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
)
async def get_order_status(token: _TOKEN) -> WebStatusResponse:
    """Return the current status of a buy_emc order by its `token`: the status, the
    exact amount, the EMC amount and destination address, and the `emc_txid` once
    delivered. Use this to poll after buy_emc — status progresses awaiting_payment →
    confirmed → emc_delivered → notified (done). Read-only; to cancel an unpaid order
    use cancel_order instead."""
    await _ready()
    with _as_tool_error():
        return await web.web_order_status(token)


@tool(
    title="Cancel an unpaid EMC order",
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def cancel_order(token: _TOKEN) -> WebStatusResponse:
    """Cancel a still-unpaid buy_emc order by its `token`, expiring it now and freeing
    its slot ahead of the TTL. Use this only before you pay; once a payment is in
    flight or confirmed it is too late and this errors. A payment sent after
    cancellation matches nothing and is NOT refunded. To only inspect an order without
    changing it, use get_order_status."""
    await _ready()
    with _as_tool_error():
        return await web.web_cancel_order(token)


if __name__ == "__main__":
    mcp.run()

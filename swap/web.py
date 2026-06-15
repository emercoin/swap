"""Public web channel — raw on-ramp for humans (swap.emercoin.com).

The service-to-service API (`/buy_emc` + signed callback, X-API-Key) stays the
primary interface: a calling service buys EMC to *its own* address so its users
never touch a wallet. This module adds a second, public surface for a person who
wants EMC at *their own* address and has no service/callback of their own.

The browser holds no secret. Orders are created under a single first-party
"web" service (its key lives only server-side) and are addressed by an opaque,
unguessable token (HMAC of the order id) — never the enumerable integer id, and
never another service's orders. Rate-limited per client IP. Web orders carry no
callback_url; the page polls GET /web/order/{token} and the watcher settles them
straight to `notified` once EMC is delivered.
"""
from __future__ import annotations

import hmac
import re
import secrets
import time
import uuid
from collections import defaultdict, deque
from hashlib import sha256

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import repository
from .config import settings
from .models import OrderStatus
from .orders import CapacityError, OrderError, ReserveError, buy_emc

router = APIRouter(prefix="/web", tags=["web"])

# Emercoin addresses: legacy base58 (P2PKH, leading 'E') or bech32 ('em1…').
_EMC_ADDR = re.compile(r"^(E[1-9A-HJ-NP-Za-km-z]{24,38}|em1[02-9ac-hj-np-z]{20,80})$")

# First-party "web" service row, resolved once at startup (see ensure_web_service).
_web_service: aiosqlite.Row | None = None

# Per-IP sliding-window rate limiter (in-memory; single-process watcher/app).
_hits: dict[str, deque[float]] = defaultdict(deque)
# Per-IP recent creations over the order-TTL window — approximates how many orders
# one client holds open at once (an order can't outlive its TTL) without tracking
# the client on each order row.
_recent: dict[str, deque[float]] = defaultdict(deque)


# --- schemas ---------------------------------------------------------------

class WebOrderRequest(BaseModel):
    amount_usdt: float = Field(..., gt=0, description="USDT to pay (within limits)")
    destination_emc_address: str = Field(..., description="your EMC address")


class WebOrderResponse(BaseModel):
    token: str = Field(..., description="opaque handle to poll this order")
    order_id: int
    deposit_address: str
    amount_usdt: float = Field(..., description="EXACT amount to send — pay this figure")
    emc_amount: float
    status: OrderStatus
    expires_at: str


class WebStatusResponse(BaseModel):
    status: OrderStatus
    amount_usdt: float
    emc_amount: float
    destination_emc_address: str
    deposit_address: str
    emc_txid: str | None = None
    expires_at: str


class WebConfigResponse(BaseModel):
    min_usdt: float
    max_usdt: float
    emc_per_usdt: float


# --- bootstrap / tokens ----------------------------------------------------

async def ensure_web_service() -> aiosqlite.Row:
    """Resolve (creating once) the first-party service that backs public orders.

    Idempotent by name; the key/secret it mints stay server-side — the secret
    also keys the order tokens below. Call at startup before serving."""
    global _web_service
    row = await repository.get_service_by_name(settings.web_service_name)
    if row is None:
        api_key = "swk_web_" + secrets.token_urlsafe(24)
        callback_secret = secrets.token_urlsafe(32)
        await repository.create_service(settings.web_service_name, api_key, callback_secret)
        row = await repository.get_service_by_name(settings.web_service_name)
    _web_service = row
    return row


def _token_for(order_id: int) -> str:
    sig = hmac.new(
        _web_service["callback_secret"].encode(), str(order_id).encode(), sha256
    ).hexdigest()[:32]
    return f"{order_id}.{sig}"


def _order_id_from_token(token: str) -> int:
    """Verify the token and return its order id, or raise 404 (don't leak which
    half was wrong)."""
    try:
        id_part, sig = token.split(".", 1)
        order_id = int(id_part)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="order not found")
    if not hmac.compare_digest(sig, _token_for(order_id).split(".", 1)[1]):
        raise HTTPException(status_code=404, detail="order not found")
    return order_id


# --- rate limit ------------------------------------------------------------

def _client_ip(request: Request) -> str:
    """Client IP, trusting the proxy's X-Forwarded-For (Caddy/CF front)."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_check(request: Request) -> None:
    ip = _client_ip(request)
    now = time.monotonic()
    window = _hits[ip]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= settings.web_rate_per_min:
        raise HTTPException(status_code=429, detail="too many requests; slow down")
    window.append(now)


def _concurrency_check(request: Request) -> None:
    """Cap how many orders one client can hold open at once. Counts this IP's
    creations within the order-TTL window (orders can't outlive it), so no per-order
    client tracking is needed; it over-counts already-settled ones, i.e. errs strict."""
    ip = _client_ip(request)
    now = time.monotonic()
    window = settings.order_ttl_minutes * 60
    recent = _recent[ip]
    while recent and now - recent[0] > window:
        recent.popleft()
    if len(recent) >= settings.web_max_concurrent_per_ip:
        raise HTTPException(status_code=429, detail="too many open orders; wait for them to expire")
    recent.append(now)


# --- endpoints -------------------------------------------------------------

@router.get("/config", response_model=WebConfigResponse)
async def web_config() -> WebConfigResponse:
    """Limits/rate for the page to render (so the form matches the server)."""
    return WebConfigResponse(
        min_usdt=settings.min_usdt,
        max_usdt=settings.max_usdt,
        emc_per_usdt=settings.emc_per_usdt,
    )


@router.post("/order", response_model=WebOrderResponse)
async def web_create_order(req: WebOrderRequest, request: Request) -> WebOrderResponse:
    """Public order creation: no key, rate-limited. One-way, exact amount, no
    refunds (per the offer). The page must show the EXACT `amount_usdt` to pay."""
    if _web_service is None:
        raise HTTPException(status_code=503, detail="web channel not ready")
    _rate_check(request)
    _concurrency_check(request)
    dest = req.destination_emc_address.strip()
    if not _EMC_ADDR.match(dest):
        raise HTTPException(status_code=400, detail="invalid EMC address")
    try:
        resp = await buy_emc(
            service_id=_web_service["id"],
            amount_usdt=req.amount_usdt,
            destination_emc_address=dest,
            callback_url="",                 # public order → page polls, no callback
            ref=uuid.uuid4().hex,            # no caller invoice id; synth a unique ref
        )
    except OrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (ReserveError, CapacityError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return WebOrderResponse(
        token=_token_for(resp.order_id),
        order_id=resp.order_id,
        deposit_address=resp.deposit_address,
        amount_usdt=resp.amount_usdt,
        emc_amount=resp.emc_amount,
        status=resp.status,
        expires_at=resp.expires_at,
    )


@router.get("/order/{token}", response_model=WebStatusResponse)
async def web_order_status(token: str) -> WebStatusResponse:
    """Poll a public order by its opaque token (no key, no id enumeration)."""
    order_id = _order_id_from_token(token)
    row = await repository.get_order(order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="order not found")
    return WebStatusResponse(
        status=OrderStatus(row["status"]),
        amount_usdt=row["amount_usdt"],
        emc_amount=row["emc_amount"],
        destination_emc_address=row["destination_emc"],
        deposit_address=settings.deposit_address,
        emc_txid=row["emc_txid"],
        expires_at=row["expires_at"],
    )

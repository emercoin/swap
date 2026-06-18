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
from .models import AmountUsdt, OrderStatus
from .orders import CapacityError, OrderError, ReserveError, buy_emc
from .services import stats

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
# Spent proof-of-work nonces → expiry epoch (anti-replay; pruned on use, bounded by
# the challenge TTL so it stays small).
_used_pow: dict[str, float] = {}


# --- schemas ---------------------------------------------------------------

class WebOrderRequest(BaseModel):
    amount_usdt: AmountUsdt
    destination_emc_address: str = Field(..., description="your EMC address")
    pow_challenge: str = Field("", description="challenge string from GET /web/challenge")
    pow_solution: str = Field("", description="solved nonce for that challenge")


class WebChallengeResponse(BaseModel):
    enabled: bool = Field(..., description="whether a solution is required")
    challenge: str
    bits: int


class WebOrderResponse(BaseModel):
    token: str = Field(..., description="opaque handle to poll this order")
    order_id: int
    deposit_address: str
    amount_usdt: float = Field(..., description="EXACT amount to send — pay this figure")
    emc_amount: float
    status: OrderStatus
    expires_at: str


class WebStatusResponse(BaseModel):
    order_id: int = Field(..., description="human-quotable order number (DB id)")
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
    allowed_amounts: list[float] = Field(
        default_factory=list, description="the fixed USDT denominations a buyer may pick"
    )
    emc_per_usdt: float
    support_email: str = Field("", description="operator contact for manual cases")


# Public stats digest (/stats.html). Balances are optional: an unreachable adapter
# or TronGrid degrades that field to null rather than failing the whole page.
class StatsEmcReserve(BaseModel):
    balance: float
    outstanding: float
    buffer: float
    available: float
    watermark: float
    low: bool


class StatsBalances(BaseModel):
    deposit_address: str
    usdt_deposit: float | None = None
    emc_reserve: StatsEmcReserve | None = None


class StatsOrders(BaseModel):
    total: int
    delivered: int
    delivered_emc: float
    by_status: dict[str, int]


class StatsWindow(BaseModel):
    created: int
    delivered: int
    delivered_emc: float


class StatsActivity(BaseModel):
    last_24h: StatsWindow
    last_7d: StatsWindow


class WebStatsResponse(BaseModel):
    generated_at: str
    balances: StatsBalances
    orders: StatsOrders
    activity: StatsActivity


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
    """Real client IP behind the edge. Prefer Cloudflare's authoritative
    `CF-Connecting-IP` (the origin must accept traffic ONLY from CF, else this header
    is client-spoofable), then the first `X-Forwarded-For` entry (Caddy), then the
    socket peer. Without this, requests behind CF collapse onto the CF edge IP and the
    per-IP caps act almost globally. NOTE: agents arriving via the Glama MCP proxy all
    share Glama's egress IP regardless — IP can't separate them (see the MCP channel)."""
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
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


def _concurrency_check(request: Request) -> str:
    """Cap how many orders one client can hold open at once, and return the client IP
    so the caller records the slot via `_concurrency_record` ONLY after the order is
    actually created — a failed creation (bad address, reserve, AML, capacity) must not
    burn a slot. Counts this IP's creations within the order-TTL window (orders can't
    outlive it), so no per-order client tracking is needed; it over-counts
    already-settled ones, i.e. errs strict."""
    ip = _client_ip(request)
    now = time.monotonic()
    window = settings.order_ttl_minutes * 60
    recent = _recent[ip]
    while recent and now - recent[0] > window:
        recent.popleft()
    if len(recent) >= settings.web_max_concurrent_per_ip:
        raise HTTPException(status_code=429, detail="too many open orders; wait for them to expire")
    return ip


def _concurrency_record(ip: str) -> None:
    """Record one open-order slot for `ip`. Call only after a successful creation, so
    failed attempts don't count. Idempotent retries (same key → same order) may slightly
    over-count, but that's bounded by the rate limit and far better than counting every
    failed attempt as the old inline append did."""
    _recent[ip].append(time.monotonic())


# --- proof of work ---------------------------------------------------------

def _new_pow_challenge() -> tuple[str, int]:
    """Mint a stateless hashcash challenge `nonce.ts.bits.sig` (sig = HMAC over the
    rest with the web secret), so we can verify it later without storing it."""
    bits = settings.web_pow_bits
    payload = f"{secrets.token_hex(8)}.{int(time.time())}.{bits}"
    sig = hmac.new(_web_service["callback_secret"].encode(), payload.encode(), sha256).hexdigest()[:16]
    return f"{payload}.{sig}", bits


def _verify_pow(challenge: str, solution: str) -> None:
    """Reject unless `solution` is a valid hashcash answer to our own, unexpired,
    not-yet-spent `challenge`. No-op when PoW is disabled."""
    if not settings.web_pow_enabled:
        return
    try:
        nonce, ts_s, bits_s, sig = challenge.split(".")
        ts, bits = int(ts_s), int(bits_s)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="invalid proof-of-work challenge")
    payload = f"{nonce}.{ts}.{bits}"
    expect = hmac.new(_web_service["callback_secret"].encode(), payload.encode(), sha256).hexdigest()[:16]
    if not hmac.compare_digest(sig, expect):
        raise HTTPException(status_code=400, detail="invalid proof-of-work challenge")
    now = time.time()
    if now - ts > settings.web_pow_ttl_seconds or ts - now > 60:
        raise HTTPException(status_code=400, detail="proof-of-work challenge expired; retry")
    for spent, exp in list(_used_pow.items()):     # prune, then reject replays
        if exp < now:
            del _used_pow[spent]
    if nonce in _used_pow:
        raise HTTPException(status_code=429, detail="proof-of-work already used; retry")
    digest = sha256(f"{challenge}.{solution}".encode()).digest()
    if int.from_bytes(digest, "big") >= (1 << (256 - bits)):
        raise HTTPException(status_code=400, detail="invalid proof-of-work solution")
    _used_pow[nonce] = ts + settings.web_pow_ttl_seconds


# --- endpoints -------------------------------------------------------------

@router.get("/config", response_model=WebConfigResponse)
async def web_config() -> WebConfigResponse:
    """Limits/rate for the page to render (so the form matches the server)."""
    return WebConfigResponse(
        min_usdt=settings.min_usdt,
        max_usdt=settings.max_usdt,
        allowed_amounts=settings.allowed_amounts,
        emc_per_usdt=settings.emc_per_usdt,
        support_email=settings.support_email,
    )


@router.get("/stats", response_model=WebStatsResponse)
async def web_stats() -> dict:
    """Public proof-of-reserves digest: USDT/EMC balances, order counts, 24h/7d
    activity. Keyless and TTL-cached (no rate gate) — read-only and safe to expose."""
    return await stats.get_stats()


@router.get("/challenge", response_model=WebChallengeResponse)
async def web_challenge() -> WebChallengeResponse:
    """Issue a proof-of-work challenge for the next order (empty when disabled)."""
    if _web_service is None:
        raise HTTPException(status_code=503, detail="web channel not ready")
    if not settings.web_pow_enabled:
        return WebChallengeResponse(enabled=False, challenge="", bits=0)
    challenge, bits = _new_pow_challenge()
    return WebChallengeResponse(enabled=True, challenge=challenge, bits=bits)


def _idempotency_ref(key: str, dest: str, amount_usdt: float) -> str:
    """Derive the order `ref` from a caller-chosen idempotency key, BOUND to the
    destination + amount. Keyless callers all share one service_id, so `ref` is the
    only namespace: hashing the destination and amount in means a key only ever
    dedups to an order with the SAME recipient and figure — two anonymous callers
    that happen to pick the same key can't be handed each other's order."""
    units = round(amount_usdt * 1_000_000)
    digest = sha256(f"{key}\x00{dest}\x00{units}".encode()).hexdigest()
    return "idem_" + digest[:40]


async def create_public_order(
    amount_usdt: float,
    destination_emc_address: str,
    request: Request,
    *,
    pow_challenge: str = "",
    pow_solution: str = "",
    enforce_pow: bool = True,
    idempotency_key: str = "",
) -> WebOrderResponse:
    """Shared keyless order creation, backing both the browser web channel and the
    MCP exchanger surface. No key: protected by per-IP rate + concurrency caps, the
    global awaiting cap and reserve pre-flight (inside `buy_emc`), and — for the
    browser — proof-of-work. Programmatic callers (MCP) set `enforce_pow=False`;
    the global cap and per-IP limits still bite. One-way, exact amount, no refunds.

    `idempotency_key` (optional): a stable, caller-chosen string that makes retries
    return the SAME order instead of opening a new one (bound to destination+amount,
    see `_idempotency_ref`). Omitted → a fresh random ref, i.e. every call is a new
    order."""
    if _web_service is None:
        raise HTTPException(status_code=503, detail="web channel not ready")
    _rate_check(request)
    ip = _concurrency_check(request)
    if enforce_pow:
        _verify_pow(pow_challenge, pow_solution)
    dest = destination_emc_address.strip()
    if not _EMC_ADDR.match(dest):
        raise HTTPException(status_code=400, detail="invalid EMC address")
    ref = _idempotency_ref(idempotency_key, dest, amount_usdt) if idempotency_key else uuid.uuid4().hex
    try:
        resp = await buy_emc(
            service_id=_web_service["id"],
            amount_usdt=amount_usdt,
            destination_emc_address=dest,
            callback_url="",                 # public order → caller polls, no callback
            ref=ref,                         # idempotency key (bound to dest+amount) or random
        )
    except OrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (ReserveError, CapacityError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    _concurrency_record(ip)              # only a real, created order burns a per-IP slot
    return WebOrderResponse(
        token=_token_for(resp.order_id),
        order_id=resp.order_id,
        deposit_address=resp.deposit_address,
        amount_usdt=resp.amount_usdt,
        emc_amount=resp.emc_amount,
        status=resp.status,
        expires_at=resp.expires_at,
    )


@router.post("/order", response_model=WebOrderResponse)
async def web_create_order(req: WebOrderRequest, request: Request) -> WebOrderResponse:
    """Public order creation: no key, rate-limited. One-way, exact amount, no
    refunds (per the offer). The page must show the EXACT `amount_usdt` to pay."""
    return await create_public_order(
        req.amount_usdt,
        req.destination_emc_address,
        request,
        pow_challenge=req.pow_challenge,
        pow_solution=req.pow_solution,
        enforce_pow=True,
    )


@router.get("/order/{token}", response_model=WebStatusResponse)
async def web_order_status(token: str) -> WebStatusResponse:
    """Poll a public order by its opaque token (no key, no id enumeration)."""
    order_id = _order_id_from_token(token)
    row = await repository.get_order(order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="order not found")
    return _status_response(row)


@router.post("/order/{token}/cancel", response_model=WebStatusResponse)
async def web_cancel_order(token: str) -> WebStatusResponse:
    """Let the buyer drop an unpaid order early (expire it now), freeing its
    awaiting slot ahead of the TTL. Only an `awaiting_payment` order can be
    cancelled; once a payment is in flight or settled it's too late (409). A
    payment that nonetheless arrives after cancellation matches no open order and,
    per the offer, is recorded unmatched and not refunded — same as a late payment."""
    order_id = _order_id_from_token(token)
    row = await repository.get_order(order_id)
    if row is None:
        raise HTTPException(status_code=404, detail="order not found")
    if OrderStatus(row["status"]) != OrderStatus.AWAITING_PAYMENT:
        raise HTTPException(status_code=409, detail="order can no longer be cancelled")
    await repository.update_status(order_id, OrderStatus.EXPIRED)
    row = await repository.get_order(order_id)
    return _status_response(row)


def _status_response(row: aiosqlite.Row) -> WebStatusResponse:
    return WebStatusResponse(
        order_id=row["id"],
        status=OrderStatus(row["status"]),
        amount_usdt=row["amount_usdt"],
        emc_amount=row["emc_amount"],
        destination_emc_address=row["destination_emc"],
        deposit_address=settings.deposit_address,
        emc_txid=row["emc_txid"],
        expires_at=row["expires_at"],
    )

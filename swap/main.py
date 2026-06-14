"""swap REST API.

Endpoints (the whole public surface):
    POST /buy_emc       — create an order, get a deposit address   (X-API-Key)
    GET  /order/{id}    — order status (caller's fallback poll)    (X-API-Key)
    GET  /healthz       — liveness

The MCP tool surface (same buy_emc + status, for agents) is mounted from
`mcp_app`. The background watcher runs as a lifespan task.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from . import db, repository, web
from .auth import Service, require_service
from .config import settings
from .models import BuyEmcRequest, BuyEmcResponse, OrderResponse, OrderStatus
from .orders import OrderError, ReserveError, buy_emc
from .services import watcher

SITE_DIR = Path(__file__).parent / "site"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("swap")

RUN_WATCHER = True  # set False in tests / API-only deployments


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()
    if settings.web_channel_enabled:
        await web.ensure_web_service()       # first-party "web" service for /web/*
    stop = asyncio.Event()
    task: asyncio.Task | None = None
    if RUN_WATCHER:
        task = asyncio.create_task(watcher.run(stop))
    try:
        yield
    finally:
        stop.set()
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
        await db.close()


app = FastAPI(
    title="swap — EMC cashier",
    version="0.1.0",
    description="USDT (TRC20) in → EMC out → signed callback. A dumb till.",
    lifespan=lifespan,
)


if settings.web_channel_enabled:
    app.include_router(web.router)           # public /web/* (raw on-ramp for humans)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.post("/buy_emc", response_model=BuyEmcResponse)
async def post_buy_emc(
    req: BuyEmcRequest, service: Service = Depends(require_service)
) -> BuyEmcResponse:
    """Create (or return idempotently) an order and its unique deposit address."""
    try:
        return await buy_emc(
            service_id=service.id,
            amount_usdt=req.amount_usdt,
            destination_emc_address=req.destination_emc_address,
            callback_url=req.callback_url,
            ref=req.ref,
        )
    except OrderError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ReserveError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/order/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: int, service: Service = Depends(require_service)
) -> OrderResponse:
    row = await repository.get_order(order_id)
    if row is None or row["service_id"] != service.id:
        raise HTTPException(status_code=404, detail="order not found")
    return OrderResponse(
        order_id=row["id"],
        ref=row["ref"],
        status=OrderStatus(row["status"]),
        amount_usdt=row["amount_usdt"],
        emc_amount=row["emc_amount"],
        destination_emc_address=row["destination_emc"],
        deposit_address=settings.deposit_address,
        emc_txid=row["emc_txid"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# Static site (exchanger page + offer). In production Caddy serves swap/site and
# only proxies the API here; locally `SWAP_SERVE_STATIC=true` lets the app serve
# it too. Mounted last so the API routes above take precedence over "/".
if settings.serve_static and SITE_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(SITE_DIR), html=True), name="site")

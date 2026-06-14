"""Caller authentication by API key.

Each calling service holds an API key (header `X-API-Key`) and a callback secret
(used to sign the callback). Both are minted at service registration; this module
just resolves a key to the active service row.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, HTTPException

from . import repository


@dataclass
class Service:
    id: int
    name: str
    callback_secret: str


async def require_service(x_api_key: str | None = Header(default=None)) -> Service:
    """FastAPI dependency: 401 unless `X-API-Key` maps to an active service."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="missing X-API-Key")
    row = await repository.get_service_by_api_key(x_api_key)
    if row is None or not row["active"]:
        raise HTTPException(status_code=401, detail="invalid API key")
    return Service(id=row["id"], name=row["name"], callback_secret=row["callback_secret"])

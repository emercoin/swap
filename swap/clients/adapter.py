"""Client for the emercoin adapter — swap's EMC payout rail.

The adapter is the only thing that speaks node RPC; swap delivers EMC by calling
its `POST /wallet/send`, gated by the shared `X-Internal-Key`. Mirrors the
adapter's contract:
    POST /wallet/send {address, amount(EMC float), comment?} -> {txid, ...}
    GET  /wallet/balance -> {balance, unconfirmed}
"""
from __future__ import annotations

import httpx

from ..config import settings


class AdapterError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"adapter {status}: {detail}")
        self.status = status
        self.detail = detail


class AdapterClient:
    def __init__(self, base_url: str | None = None, internal_key: str | None = None) -> None:
        self._base = (base_url or settings.adapter_url).rstrip("/")
        key = internal_key if internal_key is not None else settings.adapter_internal_key
        headers = {"X-Internal-Key": key} if key else {}
        self._client = httpx.AsyncClient(base_url=self._base, headers=headers, timeout=30.0)

    async def send_emc(self, address: str, amount: float, comment: str | None = None) -> str:
        """Send EMC from the hot-wallet. Returns the spending txid."""
        resp = await self._client.post(
            "/wallet/send", json={"address": address, "amount": amount, "comment": comment}
        )
        if resp.status_code >= 400:
            raise AdapterError(resp.status_code, _detail(resp))
        return resp.json()["txid"]

    async def balance(self) -> dict:
        resp = await self._client.get("/wallet/balance")
        if resp.status_code >= 400:
            raise AdapterError(resp.status_code, _detail(resp))
        return resp.json()

    async def aclose(self) -> None:
        await self._client.aclose()


def _detail(resp: httpx.Response) -> str:
    try:
        return resp.json().get("detail", resp.text)
    except ValueError:
        return resp.text

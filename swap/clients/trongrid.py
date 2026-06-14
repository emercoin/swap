"""TronGrid client — the watcher's source of TRC20 deposits.

Verified against the live mainnet API (read-only) before wiring in. Key finding:
querying transfers with `only_confirmed=true` returns **only transfers in a
solidified (irreversible) block**, i.e. TRON finality (~19-block window) is
enforced server-side. So the watcher treats every transfer this returns as
final — no per-tx block lookup or manual confirmation counting needed.

Response shape (GET /v1/accounts/{addr}/transactions/trc20):
    data[].{transaction_id, from, to, value(str), block_timestamp(ms),
            token_info.{decimals, address, symbol}}
    meta.{fingerprint, links.next, page_size}   # pagination

Start: TronGrid public API. Later: own TRON node for independence (§6).
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..config import settings


@dataclass
class Trc20Transfer:
    txid: str
    from_address: str
    to_address: str
    amount_usdt: float        # scaled from the raw integer unit by token decimals
    block_timestamp: int      # ms since epoch


def parse_transfers(data: dict) -> list[Trc20Transfer]:
    """Parse a TronGrid trc20-transfers response body into typed transfers.

    Pure function (no I/O) so the parsing is unit-tested against a captured
    real response without touching the network.
    """
    out: list[Trc20Transfer] = []
    for t in data.get("data", []):
        info = t.get("token_info") or {}
        decimals = int(info.get("decimals", 6))
        try:
            amount = int(t["value"]) / (10 ** decimals)
        except (KeyError, ValueError, TypeError):
            continue  # malformed row — skip rather than crash the scan
        out.append(
            Trc20Transfer(
                txid=t["transaction_id"],
                from_address=t["from"],
                to_address=t["to"],
                amount_usdt=amount,
                block_timestamp=int(t.get("block_timestamp", 0)),
            )
        )
    return out


class TronGridClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        self._base = (base_url or settings.trongrid_url).rstrip("/")
        key = api_key if api_key is not None else settings.trongrid_api_key
        headers = {"TRON-PRO-API-KEY": key} if key else {}
        self._client = httpx.AsyncClient(base_url=self._base, headers=headers, timeout=30.0)

    async def usdt_transfers_to(
        self, address: str, *, min_timestamp: int = 0, limit: int = 50
    ) -> list[Trc20Transfer]:
        """Final (irreversible) incoming USDT transfers to `address`.

        `only_confirmed=true` => solidified block only => already final. A deposit
        address is unique per order, so everything returned belongs to that order.
        """
        params: dict[str, str | int] = {
            "only_to": "true",
            "only_confirmed": "true",
            "contract_address": settings.usdt_contract,
            "order_by": "block_timestamp,asc",
            "limit": limit,
        }
        if min_timestamp:
            params["min_timestamp"] = min_timestamp
        resp = await self._client.get(
            f"/v1/accounts/{address}/transactions/trc20", params=params
        )
        resp.raise_for_status()
        return parse_transfers(resp.json())

    async def block_height(self) -> int:
        """Current head block number (diagnostics / sweep gas checks)."""
        resp = await self._client.post("/wallet/getnowblock")
        resp.raise_for_status()
        return resp.json()["block_header"]["raw_data"]["number"]

    async def aclose(self) -> None:
        await self._client.aclose()

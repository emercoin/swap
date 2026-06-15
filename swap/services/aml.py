"""AML screening of the incoming sender address.

Screening is mandatory: a tainted USDT deposit can be frozen by Tether directly
on our address, so screening protects working capital. Two sources:
  - **OFAC SDN** — a finite published list of sanctioned addresses, loaded into
    memory (`load_blacklists`) and refreshed periodically. Pure in-memory lookup.
  - **Tether freeze** — checked **live** per deposit via the USDT contract's
    `isBlackListed` (the contract is the authoritative, always-current source;
    no stale list to maintain).

A hit → order goes to `aml_hold` for manual review; EMC is never delivered.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from ..config import settings

log = logging.getLogger("swap.aml")

# In-memory OFAC (and any other static) blacklist: address -> source tag.
_blacklist: dict[str, str] = {}


@dataclass
class AmlResult:
    clear: bool
    source: str | None = None  # which list matched, when not clear


def screen(address: str) -> AmlResult:
    """Pure, synchronous lookup against the in-memory (OFAC) blacklist."""
    src = _blacklist.get(address)
    return AmlResult(clear=src is None, source=src)


async def screen_full(address: str, tron) -> AmlResult:
    """Full screen: in-memory OFAC first, then the live Tether freeze check.

    The live check is best-effort: if it errors (network, or a testnet token with
    no `isBlackListed`), we log and treat it as clear rather than wedging every
    order — the OFAC list still applies and a stuck check shouldn't halt the till.
    """
    res = screen(address)
    if not res.clear:
        return res
    if settings.aml_tether_check:
        try:
            if await tron.usdt_is_blacklisted(address):
                return AmlResult(clear=False, source="tether")
        except Exception as exc:
            log.warning("AML: Tether check failed for %s (treating as clear): %s", address, exc)
    return AmlResult(clear=True)


def parse_ofac(text: str) -> list[str]:
    """Extract TRON base58 addresses from the OFAC per-chain list (one per line)."""
    return [a for a in (line.strip() for line in text.splitlines())
            if a.startswith("T") and len(a) == 34]


async def load_ofac() -> int:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(settings.aml_ofac_url)
        resp.raise_for_status()
    count = 0
    for addr in parse_ofac(resp.text):
        _blacklist[addr] = "ofac"
        count += 1
    return count


async def load_blacklists() -> int:
    """Load the static OFAC list into memory. Tether freezes are screened live
    per deposit (see `screen_full`), not preloaded. Returns addresses loaded."""
    n = await load_ofac()
    log.info("AML: loaded %d OFAC addresses", n)
    return n

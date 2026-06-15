"""Pre-cutover readiness check for swap.

    uv run python -m scripts.preflight

Reads the active settings (.env) and verifies the deployment is internally
consistent and ready to take real payments: the TRON network and USDT contract
agree, the deposit address is valid and unused, TronGrid and the EMC adapter are
reachable, the EMC reserve can cover an order, and no stale (testnet) orders
remain in the DB. Read-only — it changes nothing. Exits non-zero on any FAIL.
"""
from __future__ import annotations

import asyncio
import sys

from bip_utils import Base58Decoder

from swap import db
from swap.clients.adapter import AdapterClient, AdapterError
from swap.clients.trongrid import TronGridClient
from swap.config import settings
from swap.repository import outstanding_emc

MAINNET_USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_MARK = {PASS: "✓", WARN: "!", FAIL: "✗"}


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, level: str, name: str, detail: str = "") -> None:
        self.rows.append((level, name, detail))

    def worst(self) -> str:
        levels = {level for level, _, _ in self.rows}
        if FAIL in levels:
            return FAIL
        if WARN in levels:
            return WARN
        return PASS


def _url_network(url: str) -> str:
    u = url.lower()
    if "nile" in u:
        return "nile"
    if "shasta" in u:
        return "shasta"
    if "api.trongrid.io" in u:
        return "mainnet"
    return "unknown"


def check_network(r: Report) -> None:
    net = _url_network(settings.trongrid_url)
    mainnet_contract = settings.usdt_contract == MAINNET_USDT
    if net == "mainnet" and mainnet_contract:
        r.add(PASS, "TRON network", "mainnet TronGrid URL + mainnet USDT contract")
    elif net == "mainnet" and not mainnet_contract:
        r.add(FAIL, "TRON network", "mainnet TronGrid URL but USDT contract is not mainnet USDT")
    elif net in ("nile", "shasta") and mainnet_contract:
        r.add(FAIL, "TRON network", f"{net} TronGrid URL but mainnet USDT contract")
    elif net in ("nile", "shasta"):
        r.add(WARN, "TRON network", f"{net} URL + non-mainnet contract (still testnet)")
    else:
        r.add(WARN, "TRON network", f"can't classify TronGrid URL {settings.trongrid_url}")

    if not settings.trongrid_api_key:
        r.add(WARN, "TronGrid API key", "empty — the public endpoint is rate-limited (429)")
    else:
        r.add(PASS, "TronGrid API key", "set")


def check_deposit_address(r: Report) -> None:
    addr = settings.deposit_address
    if not addr:
        r.add(FAIL, "Deposit address", "SWAP_DEPOSIT_ADDRESS is empty")
        return
    try:
        Base58Decoder.CheckDecode(addr)
    except Exception:
        r.add(FAIL, "Deposit address", f"not a valid base58check TRON address: {addr}")
        return
    if addr.startswith("T"):
        r.add(PASS, "Deposit address", addr)
    else:
        r.add(WARN, "Deposit address", f"decodes but doesn't look like a TRON address: {addr}")


def check_secrets(r: Report) -> None:
    if settings.tron_mnemonic:
        r.add(WARN, "Spend key", "SWAP_TRON_MNEMONIC is set on this host — keep it offline; the watcher only reads TRON")
    else:
        r.add(PASS, "Spend key", "mnemonic not on host (read-only watcher)")
    if not settings.adapter_internal_key:
        r.add(FAIL, "Adapter key", "SWAP_ADAPTER_INTERNAL_KEY is empty")
    else:
        r.add(PASS, "Adapter key", "set")


def check_economics(r: Report) -> None:
    if 0 < settings.min_usdt <= settings.max_usdt:
        r.add(PASS, "Economics", f"{settings.min_usdt}-{settings.max_usdt} USDT @ x{settings.emc_per_usdt}")
    else:
        r.add(FAIL, "Economics", f"min/max misconfigured: {settings.min_usdt}/{settings.max_usdt}")


async def check_db(r: Report) -> None:
    conn = await db.get_conn()
    async with conn.execute("SELECT COUNT(*) FROM orders") as cur:
        (n,) = await cur.fetchone()
    if n == 0:
        r.add(PASS, "DB clean", "no existing orders")
    else:
        r.add(WARN, "DB clean", f"{n} existing order(s) — wipe testnet/staging history before mainnet")


async def check_tron(r: Report) -> None:
    if not settings.deposit_address:
        return
    cli = TronGridClient()
    try:
        try:
            height = await cli.block_height()
            r.add(PASS, "TronGrid reachable", f"head block {height}")
        except Exception as exc:
            r.add(FAIL, "TronGrid reachable", str(exc))
            return
        try:
            transfers = await cli.usdt_transfers_to(settings.deposit_address, limit=1)
            if transfers:
                r.add(WARN, "Deposit address unused",
                      f"already has USDT history (tx {transfers[0].txid[:12]}...) — use a fresh address")
            else:
                r.add(PASS, "Deposit address unused", "no prior USDT transfers")
        except Exception as exc:
            r.add(WARN, "Deposit address unused", f"could not check history: {exc}")
        try:
            await cli.usdt_is_blacklisted(settings.deposit_address)
            r.add(PASS, "Tether freeze check", "isBlackListed() callable on the USDT contract")
        except Exception as exc:
            r.add(WARN, "Tether freeze check",
                  f"isBlackListed() not callable ({exc}) — AML Tether check won't work on this token")
    finally:
        await cli.aclose()


async def check_reserve(r: Report) -> None:
    if not settings.emc_reserve_check:
        r.add(WARN, "EMC reserve", "reserve check disabled (SWAP_EMC_RESERVE_CHECK=false)")
    adapter = AdapterClient()
    try:
        try:
            balance = float((await adapter.balance())["balance"])
        except (AdapterError, Exception) as exc:
            r.add(FAIL, "Adapter reachable", str(exc))
            return
        r.add(PASS, "Adapter reachable", f"EMC balance {balance:.4f}")
        available = balance - await outstanding_emc() - settings.emc_reserve_buffer
        need = settings.max_usdt * settings.emc_per_usdt
        if available >= need:
            r.add(PASS, "EMC reserve", f"available {available:.4f} EMC >= one max order ({need:.4f})")
        else:
            r.add(WARN, "EMC reserve", f"available {available:.4f} EMC < one max order ({need:.4f}) — top up")
    finally:
        await adapter.aclose()


async def main() -> None:
    r = Report()
    check_network(r)
    check_deposit_address(r)
    check_secrets(r)
    check_economics(r)
    await db.connect()
    await check_db(r)
    await check_tron(r)
    await check_reserve(r)
    await db.close()

    print("\nswap preflight\n" + "-" * 14)
    for level, name, detail in r.rows:
        print(f"  {_MARK[level]} {name:<22} {detail}")
    verdict = r.worst()
    print(f"\nverdict: {verdict}")
    if verdict == FAIL:
        print("Resolve the FAIL items before taking real payments.")
        sys.exit(1)
    if verdict == WARN:
        print("Review the WARN items; none block startup.")


if __name__ == "__main__":
    asyncio.run(main())

"""watcher._scan_payments: deposit detection → AML → amount branch."""
import pytest

from swap import db, repository
from swap.clients.trongrid import Trc20Transfer
from swap.models import OrderStatus
from swap.services import aml, watcher


class FakeTron:
    """Stand-in TronGrid: preset transfers per address + a set of frozen senders."""
    def __init__(self, by_address: dict[str, list[Trc20Transfer]], frozen: set[str] | None = None):
        self._by = by_address
        self._frozen = frozen or set()

    async def usdt_transfers_to(self, address, **_):
        return self._by.get(address, [])

    async def usdt_is_blacklisted(self, address):
        return address in self._frozen


def _xfer(frm: str, amount: float, txid: str) -> Trc20Transfer:
    return Trc20Transfer(txid=txid, from_address=frm, to_address="Tdep",
                         amount_usdt=amount, block_timestamp=1)


async def _awaiting_order(amount_usdt: float, address: str, ref: str) -> int:
    sid = await repository.create_service(f"svc-{ref}", f"k-{ref}", "secret")
    oid = await repository.insert_order(
        service_id=sid, ref=ref, amount_usdt=amount_usdt, emc_amount=amount_usdt * 10,
        destination_emc="EMCdest", callback_url="http://svc/cb",
        expires_at="2999-01-01T00:00:00Z",
    )
    await repository.set_deposit_address(oid, address)   # → awaiting_payment
    return oid


async def test_exact_payment_confirms(fresh_db):
    oid = await _awaiting_order(5.0, "Tdep1", "exact")
    tron = FakeTron({"Tdep1": [_xfer("Tsender", 5.0, "tx1")]})
    await watcher._scan_payments(tron)
    order = await repository.get_order(oid)
    assert order["status"] == OrderStatus.CONFIRMED


async def test_underpayment(fresh_db):
    oid = await _awaiting_order(5.0, "Tdep2", "under")
    tron = FakeTron({"Tdep2": [_xfer("Tsender", 4.0, "tx2")]})
    await watcher._scan_payments(tron)
    assert (await repository.get_order(oid))["status"] == OrderStatus.UNDERPAID


async def test_overpayment_sums_transfers(fresh_db):
    oid = await _awaiting_order(5.0, "Tdep3", "over")
    tron = FakeTron({"Tdep3": [_xfer("Ts", 3.0, "tx3a"), _xfer("Ts", 3.0, "tx3b")]})
    await watcher._scan_payments(tron)
    assert (await repository.get_order(oid))["status"] == OrderStatus.OVERPAID


async def test_ofac_sender_holds_even_if_exact(fresh_db, monkeypatch):
    monkeypatch.setattr(aml, "_blacklist", {"Tbad": "ofac"})
    oid = await _awaiting_order(5.0, "Tdep4", "ofac")
    tron = FakeTron({"Tdep4": [_xfer("Tbad", 5.0, "tx4")]})
    await watcher._scan_payments(tron)
    order = await repository.get_order(oid)
    assert order["status"] == OrderStatus.AML_HOLD       # exact amount, but held


async def test_tether_frozen_sender_holds(fresh_db, monkeypatch):
    monkeypatch.setattr(aml, "_blacklist", {})           # clear OFAC list
    oid = await _awaiting_order(5.0, "Tdep6", "tether")
    # sender not in OFAC, but the live Tether check says frozen
    tron = FakeTron({"Tdep6": [_xfer("Tfrozen", 5.0, "tx6")]}, frozen={"Tfrozen"})
    await watcher._scan_payments(tron)
    assert (await repository.get_order(oid))["status"] == OrderStatus.AML_HOLD


async def test_deposit_is_recorded(fresh_db):
    oid = await _awaiting_order(5.0, "Tdep5", "rec")
    tron = FakeTron({"Tdep5": [_xfer("Tsender", 5.0, "tx5")]})
    await watcher._scan_payments(tron)
    conn = await db.get_conn()
    cur = await conn.execute("SELECT * FROM deposits WHERE order_id = ?", (oid,))
    rows = await cur.fetchall()
    assert len(rows) == 1 and rows[0]["tron_txid"] == "tx5"

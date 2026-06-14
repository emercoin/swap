"""watcher._scan_payments: shared address, exact-amount match → AML → confirm."""
import pytest

from swap import db, repository
from swap.clients.trongrid import Trc20Transfer
from swap.config import settings
from swap.models import OrderStatus
from swap.services import aml, watcher

DEPOSIT = "TSharedDepositAddr"


@pytest.fixture
def deposit_addr(monkeypatch):
    monkeypatch.setattr(settings, "deposit_address", DEPOSIT)


class FakeTron:
    """Returns preset transfers for the shared address + a set of frozen senders."""
    def __init__(self, transfers, frozen=None):
        self._transfers = transfers
        self._frozen = frozen or set()

    async def usdt_transfers_to(self, address, **_):
        return self._transfers if address == DEPOSIT else []

    async def usdt_is_blacklisted(self, address):
        return address in self._frozen


def _xfer(frm, amount, txid):
    return Trc20Transfer(txid=txid, from_address=frm, to_address=DEPOSIT,
                         amount_usdt=amount, block_timestamp=1)


async def _order(amount_usdt: float, ref: str) -> int:
    sid = await repository.create_service(f"svc-{ref}", f"k-{ref}", "secret")
    return await repository.insert_order(
        service_id=sid, ref=ref, amount_usdt=amount_usdt, emc_amount=amount_usdt * 10,
        destination_emc="E", callback_url="c", expires_at="2999-01-01T00:00:00Z",
    )


async def test_exact_amount_confirms(fresh_db, deposit_addr):
    oid = await _order(5.000003, "exact")
    await watcher._scan_payments(FakeTron([_xfer("Tsender", 5.000003, "tx1")]))
    assert (await repository.get_order(oid))["status"] == OrderStatus.CONFIRMED


async def test_wrong_amount_is_unmatched(fresh_db, deposit_addr):
    oid = await _order(5.000003, "wrong")
    await watcher._scan_payments(FakeTron([_xfer("Tsender", 5.0, "tx2")]))  # no order at 5.0
    assert (await repository.get_order(oid))["status"] == OrderStatus.AWAITING_PAYMENT
    conn = await db.get_conn()
    cur = await conn.execute("SELECT order_id FROM deposits WHERE tron_txid='tx2'")
    row = await cur.fetchone()
    assert row is not None and row["order_id"] is None      # recorded as unmatched


async def test_ofac_sender_holds(fresh_db, deposit_addr, monkeypatch):
    monkeypatch.setattr(aml, "_blacklist", {"Tbad": "ofac"})
    oid = await _order(5.000004, "ofac")
    await watcher._scan_payments(FakeTron([_xfer("Tbad", 5.000004, "tx3")]))
    assert (await repository.get_order(oid))["status"] == OrderStatus.AML_HOLD


async def test_tether_frozen_sender_holds(fresh_db, deposit_addr, monkeypatch):
    monkeypatch.setattr(aml, "_blacklist", {})
    oid = await _order(5.000005, "tether")
    await watcher._scan_payments(FakeTron([_xfer("Tfrozen", 5.000005, "tx4")], frozen={"Tfrozen"}))
    assert (await repository.get_order(oid))["status"] == OrderStatus.AML_HOLD


async def test_already_seen_txid_skipped(fresh_db, deposit_addr):
    oid = await _order(5.000006, "seen")
    tron = FakeTron([_xfer("Ts", 5.000006, "tx5")])
    await watcher._scan_payments(tron)          # confirms
    await watcher._scan_payments(tron)          # re-seen tx5 → no-op, no error
    assert (await repository.get_order(oid))["status"] == OrderStatus.CONFIRMED

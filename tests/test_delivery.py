"""EMC delivery: failure → deliver_failed (+attempt count) → recovers on retry."""
import pytest

from swap import repository
from swap.clients.adapter import AdapterError
from swap.models import OrderStatus
from swap.services import delivery


class FailingAdapter:
    async def send_emc(self, address, amount, comment=None):
        raise AdapterError(400, "insufficient funds")        # depleted EMC reserve


class OkAdapter:
    def __init__(self):
        self.sent = []

    async def send_emc(self, address, amount, comment=None):
        self.sent.append((address, amount))
        return "emc-txid-1"


async def _confirmed_order() -> int:
    sid = await repository.create_service("svc", "k", "s")
    oid = await repository.insert_order(
        service_id=sid, ref="r", amount_usdt=5.0, emc_amount=50.0,
        destination_emc="EMCdest", callback_url="c", expires_at="2999-01-01T00:00:00Z",
    )
    await repository.update_status(oid, OrderStatus.CONFIRMED)
    return oid


async def test_failure_marks_deliver_failed_and_counts(fresh_db):
    oid = await _confirmed_order()
    with pytest.raises(AdapterError):
        await delivery.deliver(oid, FailingAdapter())
    o = await repository.get_order(oid)
    assert o["status"] == OrderStatus.DELIVER_FAILED
    assert o["delivery_attempts"] == 1
    assert o["emc_txid"] is None


async def test_recovers_after_topup(fresh_db):
    oid = await _confirmed_order()
    with pytest.raises(AdapterError):
        await delivery.deliver(oid, FailingAdapter())        # reserve empty
    txid = await delivery.deliver(oid, OkAdapter())          # topped up → retry works
    o = await repository.get_order(oid)
    assert txid == "emc-txid-1"
    assert o["status"] == OrderStatus.EMC_DELIVERED
    assert o["emc_txid"] == "emc-txid-1"


async def test_already_delivered_is_idempotent(fresh_db):
    oid = await _confirmed_order()
    await delivery.deliver(oid, OkAdapter())
    ok = OkAdapter()
    txid = await delivery.deliver(oid, ok)                   # second call: no re-send
    assert txid == "emc-txid-1" and ok.sent == []
import pytest

from swap import orders, repository
from swap.config import settings
from swap.models import OrderStatus


@pytest.fixture
def deposit_addr(monkeypatch):
    monkeypatch.setattr(settings, "deposit_address", "TSharedDepositAddr")
    monkeypatch.setattr(settings, "emc_reserve_check", False)   # off unless a test opts in


class FakeAdapter:
    def __init__(self, balance: float):
        self._balance = balance

    async def balance(self):
        return {"balance": self._balance, "unconfirmed": 0}


async def _service():
    return await repository.create_service("svc", "swk_test", "secret")


async def test_buy_emc_creates_order(fresh_db, deposit_addr):
    sid = await _service()
    resp = await orders.buy_emc(
        service_id=sid, amount_usdt=5.0,
        destination_emc_address="EMCdest", callback_url="http://svc/cb", ref="inv-1",
    )
    assert resp.order_id > 0
    assert resp.deposit_address == "TSharedDepositAddr"   # one shared address
    assert resp.amount_usdt == 5.0                        # first order: no tag bump
    assert resp.emc_amount == 50.0                        # ×10 fixed rate
    assert resp.status == OrderStatus.AWAITING_PAYMENT


async def test_unique_amount_tag(fresh_db, deposit_addr):
    sid = await _service()
    a = await orders.buy_emc(service_id=sid, amount_usdt=5.0,
                             destination_emc_address="E", callback_url="c", ref="r1")
    b = await orders.buy_emc(service_id=sid, amount_usdt=5.0,
                             destination_emc_address="E", callback_url="c", ref="r2")
    assert a.amount_usdt == 5.0
    assert b.amount_usdt == 5.000001          # bumped one micro-USDT to stay unique
    assert a.order_id != b.order_id


async def test_buy_emc_is_idempotent_on_ref(fresh_db, deposit_addr):
    sid = await _service()
    a = await orders.buy_emc(service_id=sid, amount_usdt=5.0,
                             destination_emc_address="E", callback_url="c", ref="dup")
    b = await orders.buy_emc(service_id=sid, amount_usdt=5.0,
                             destination_emc_address="E", callback_url="c", ref="dup")
    assert a.order_id == b.order_id and a.amount_usdt == b.amount_usdt


async def test_tag_reused_after_order_terminal(fresh_db, deposit_addr):
    """A terminal (e.g. expired) order releases its amount tag, so the next order
    reuses the smallest free figure instead of creeping upward forever."""
    sid = await _service()
    a = await orders.buy_emc(service_id=sid, amount_usdt=5.0,
                             destination_emc_address="E", callback_url="c", ref="r1")
    b = await orders.buy_emc(service_id=sid, amount_usdt=5.0,
                             destination_emc_address="E", callback_url="c", ref="r2")
    assert (a.amount_usdt, b.amount_usdt) == (5.0, 5.000001)   # both active → b bumped

    await repository.update_status(a.order_id, OrderStatus.EXPIRED)   # frees the 5.0 tag
    c = await orders.buy_emc(service_id=sid, amount_usdt=5.0,
                             destination_emc_address="E", callback_url="c", ref="r3")
    assert c.amount_usdt == 5.0          # smallest free tag reused, NOT 5.000002


async def test_active_order_keeps_its_tag_reserved(fresh_db, deposit_addr):
    """A paid-but-undelivered (active, non-terminal) order must NOT release its tag,
    or a duplicate payment could be mis-matched."""
    sid = await _service()
    a = await orders.buy_emc(service_id=sid, amount_usdt=5.0,
                             destination_emc_address="E", callback_url="c", ref="r1")
    await repository.update_status(a.order_id, OrderStatus.CONFIRMED)   # active, mid-settlement
    b = await orders.buy_emc(service_id=sid, amount_usdt=5.0,
                             destination_emc_address="E", callback_url="c", ref="r2")
    assert b.amount_usdt == 5.000001     # 5.0 still held by the confirmed order


async def test_buy_emc_enforces_cap(fresh_db, deposit_addr):
    sid = await _service()
    with pytest.raises(orders.OrderError):
        await orders.buy_emc(service_id=sid, amount_usdt=11.0,
                             destination_emc_address="E", callback_url="c", ref="big")


async def test_buy_emc_enforces_minimum(fresh_db, deposit_addr):
    sid = await _service()
    with pytest.raises(orders.OrderError):
        await orders.buy_emc(service_id=sid, amount_usdt=0.5,   # min is 5
                             destination_emc_address="E", callback_url="c", ref="dust")


async def test_buy_emc_requires_deposit_address(fresh_db, monkeypatch):
    monkeypatch.setattr(settings, "deposit_address", "")
    sid = await _service()
    with pytest.raises(orders.OrderError):
        await orders.buy_emc(service_id=sid, amount_usdt=5.0,
                             destination_emc_address="E", callback_url="c", ref="x")


async def test_reserve_rejects_when_low(fresh_db, deposit_addr, monkeypatch):
    monkeypatch.setattr(settings, "emc_reserve_check", True)
    sid = await _service()
    with pytest.raises(orders.ReserveError):     # needs 50 EMC, wallet has 40
        await orders.buy_emc(service_id=sid, amount_usdt=5.0, destination_emc_address="E",
                             callback_url="c", ref="low", adapter=FakeAdapter(40))


async def test_reserve_ignores_unpaid_orders(fresh_db, deposit_addr, monkeypatch):
    # Unpaid awaiting orders must NOT consume the reserve, else anyone could 503
    # every real buyer just by creating orders they never pay for. Wallet holds 70
    # (enough for one 50 EMC order); many unpaid orders are still all allowed.
    monkeypatch.setattr(settings, "emc_reserve_check", True)
    sid = await _service()
    for i in range(5):
        resp = await orders.buy_emc(service_id=sid, amount_usdt=5.0, destination_emc_address="E",
                                    callback_url="c", ref=f"u{i}", adapter=FakeAdapter(70))
        assert resp.status == OrderStatus.AWAITING_PAYMENT


async def test_reserve_accounts_for_paid_orders(fresh_db, deposit_addr, monkeypatch):
    # A confirmed (paid) order DOES reserve its EMC: wallet 70 owes 50 → only 20
    # free, so the next 50 EMC order can't be covered.
    monkeypatch.setattr(settings, "emc_reserve_check", True)
    sid = await _service()
    first = await orders.buy_emc(service_id=sid, amount_usdt=5.0, destination_emc_address="E",
                                 callback_url="c", ref="paid", adapter=FakeAdapter(70))
    await repository.update_status(first.order_id, OrderStatus.CONFIRMED)
    with pytest.raises(orders.ReserveError):
        await orders.buy_emc(service_id=sid, amount_usdt=5.0, destination_emc_address="E",
                             callback_url="c", ref="next", adapter=FakeAdapter(70))


async def test_global_awaiting_cap(fresh_db, deposit_addr, monkeypatch):
    # The global back-pressure cap rejects new orders once too many are awaiting,
    # so order-creation spam can't exhaust the tag space. Repeat refs still resolve.
    monkeypatch.setattr(settings, "max_awaiting_orders", 2)
    sid = await _service()
    for i in range(2):
        await orders.buy_emc(service_id=sid, amount_usdt=5.0, destination_emc_address="E",
                             callback_url="c", ref=f"c{i}")
    with pytest.raises(orders.CapacityError):
        await orders.buy_emc(service_id=sid, amount_usdt=5.0, destination_emc_address="E",
                             callback_url="c", ref="over")
    # an existing order is still returned even at the cap (idempotency wins)
    again = await orders.buy_emc(service_id=sid, amount_usdt=5.0, destination_emc_address="E",
                                 callback_url="c", ref="c0")
    assert again.order_id > 0


async def test_reserve_allows_when_enough(fresh_db, deposit_addr, monkeypatch):
    monkeypatch.setattr(settings, "emc_reserve_check", True)
    sid = await _service()
    resp = await orders.buy_emc(service_id=sid, amount_usdt=5.0, destination_emc_address="E",
                                callback_url="c", ref="ok", adapter=FakeAdapter(1000))
    assert resp.status == OrderStatus.AWAITING_PAYMENT

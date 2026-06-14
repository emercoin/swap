import pytest

from swap import orders, repository
from swap.models import OrderStatus


@pytest.fixture
def patched_hd(monkeypatch):
    # Avoid needing a real mnemonic / bip_utils: deterministic fake address per index.
    monkeypatch.setattr(orders, "derive_deposit_address", lambda i: f"TFAKE{i:034d}")


async def _service():
    return await repository.create_service("svc", "swk_test", "secret")


async def test_buy_emc_creates_order(fresh_db, patched_hd):
    sid = await _service()
    resp = await orders.buy_emc(
        service_id=sid, amount_usdt=5.0,
        destination_emc_address="EMCdest", callback_url="http://svc/cb", ref="inv-1",
    )
    assert resp.order_id > 0
    assert resp.emc_amount == 50.0          # ×10 fixed rate
    assert resp.deposit_address == f"TFAKE{resp.order_id:034d}"
    assert resp.status == OrderStatus.AWAITING_PAYMENT


async def test_buy_emc_is_idempotent_on_ref(fresh_db, patched_hd):
    sid = await _service()
    a = await orders.buy_emc(
        service_id=sid, amount_usdt=5.0,
        destination_emc_address="EMCdest", callback_url="http://svc/cb", ref="dup",
    )
    b = await orders.buy_emc(
        service_id=sid, amount_usdt=5.0,
        destination_emc_address="EMCdest", callback_url="http://svc/cb", ref="dup",
    )
    assert a.order_id == b.order_id         # same ref → same order, no double deposit


async def test_buy_emc_enforces_cap(fresh_db, patched_hd):
    sid = await _service()
    with pytest.raises(orders.OrderError):
        await orders.buy_emc(
            service_id=sid, amount_usdt=11.0,  # cap is 10
            destination_emc_address="EMCdest", callback_url="http://svc/cb", ref="big",
        )


async def test_buy_emc_enforces_minimum(fresh_db, patched_hd):
    sid = await _service()
    with pytest.raises(orders.OrderError):
        await orders.buy_emc(
            service_id=sid, amount_usdt=0.5,   # min is 1
            destination_emc_address="EMCdest", callback_url="http://svc/cb", ref="dust",
        )

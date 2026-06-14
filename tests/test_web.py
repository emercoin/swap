"""Public web channel: first-party service bootstrap, opaque tokens, order
creation without a key, rate limiting, and the no-callback settle path."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from swap import repository, web
from swap.config import settings
from swap.models import OrderStatus


@pytest.fixture
def web_env(monkeypatch):
    monkeypatch.setattr(settings, "deposit_address", "TSharedDepositAddr")
    monkeypatch.setattr(settings, "emc_reserve_check", False)
    monkeypatch.setattr(settings, "web_rate_per_min", 3)
    web._hits.clear()
    web._web_service = None


def _req(ip="1.2.3.4", xff=None):
    headers = {"x-forwarded-for": xff} if xff else {}
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=ip))


async def test_ensure_web_service_idempotent(fresh_db, web_env):
    a = await web.ensure_web_service()
    b = await web.ensure_web_service()
    assert a["id"] == b["id"]
    assert a["name"] == settings.web_service_name
    # only one row, regardless of repeated bootstrap
    assert (await repository.get_service_by_name(settings.web_service_name))["id"] == a["id"]


async def test_token_roundtrip_and_tamper(fresh_db, web_env):
    await web.ensure_web_service()
    tok = web._token_for(42)
    assert web._order_id_from_token(tok) == 42
    for bad in (tok[:-1] + ("0" if tok[-1] != "0" else "1"), "42.deadbeef", "nope", "42"):
        with pytest.raises(HTTPException) as e:
            web._order_id_from_token(bad)
        assert e.value.status_code == 404


async def test_web_create_order_no_key(fresh_db, web_env):
    await web.ensure_web_service()
    req = web.WebOrderRequest(
        amount_usdt=5.0,
        destination_emc_address="em1qamp64pg6ye8j6h2gszs6k0y7u3p9hkstcy65s6",
    )
    resp = await web.web_create_order(req, _req())
    assert resp.order_id > 0
    assert resp.deposit_address == "TSharedDepositAddr"
    assert resp.emc_amount == 50.0
    assert web._order_id_from_token(resp.token) == resp.order_id
    # order is owned by the web service and carries NO callback_url
    row = await repository.get_order(resp.order_id)
    assert row["service_id"] == web._web_service["id"]
    assert row["callback_url"] == ""


async def test_web_create_order_rejects_bad_address(fresh_db, web_env):
    await web.ensure_web_service()
    req = web.WebOrderRequest(amount_usdt=5.0, destination_emc_address="not an address!!")
    with pytest.raises(HTTPException) as e:
        await web.web_create_order(req, _req())
    assert e.value.status_code == 400


async def test_web_status_by_token(fresh_db, web_env):
    await web.ensure_web_service()
    req = web.WebOrderRequest(
        amount_usdt=5.0, destination_emc_address="Eeyqx7K8Yk9mWcCq4zZ6vUe2gJ3hNpRtAa"
    )
    created = await web.web_create_order(req, _req())
    status = await web.web_order_status(created.token)
    assert status.status == OrderStatus.AWAITING_PAYMENT
    assert status.amount_usdt == created.amount_usdt
    assert status.deposit_address == "TSharedDepositAddr"


async def test_rate_limit_per_ip(fresh_db, web_env):
    await web.ensure_web_service()
    req = _req(ip="9.9.9.9")
    for _ in range(settings.web_rate_per_min):
        web._rate_check(req)
    with pytest.raises(HTTPException) as e:
        web._rate_check(req)
    assert e.value.status_code == 429
    # a different IP is unaffected
    web._rate_check(_req(ip="8.8.8.8"))


async def test_client_ip_prefers_forwarded_for(fresh_db, web_env):
    assert web._client_ip(_req(ip="10.0.0.1", xff="203.0.113.7, 10.0.0.1")) == "203.0.113.7"
    assert web._client_ip(_req(ip="10.0.0.1")) == "10.0.0.1"

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
    monkeypatch.setattr(settings, "web_pow_enabled", False)   # on only in PoW tests
    web._hits.clear()
    web._recent.clear()
    web._used_pow.clear()
    web._web_service = None


def _solve(challenge: str, bits: int) -> str:
    """Mirror the browser solver: find a nonce meeting the hashcash difficulty."""
    import hashlib
    target = 1 << (256 - bits)
    sol = 0
    while int.from_bytes(hashlib.sha256(f"{challenge}.{sol}".encode()).digest(), "big") >= target:
        sol += 1
    return str(sol)


def _req(ip="1.2.3.4", xff=None, cf=None):
    headers = {}
    if xff:
        headers["x-forwarded-for"] = xff
    if cf:
        headers["cf-connecting-ip"] = cf
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


async def test_web_cancel_awaiting_order(fresh_db, web_env):
    await web.ensure_web_service()
    req = web.WebOrderRequest(
        amount_usdt=5.0, destination_emc_address="em1qamp64pg6ye8j6h2gszs6k0y7u3p9hkstcy65s6"
    )
    created = await web.web_create_order(req, _req())
    resp = await web.web_cancel_order(created.token)
    assert resp.status == OrderStatus.EXPIRED
    row = await repository.get_order(created.order_id)
    assert row["status"] == OrderStatus.EXPIRED
    # the freed tag no longer matches an incoming payment
    assert await repository.find_open_order_by_amount(created.amount_usdt) is None


async def test_web_cancel_rejects_non_awaiting(fresh_db, web_env):
    await web.ensure_web_service()
    req = web.WebOrderRequest(
        amount_usdt=5.0, destination_emc_address="em1qamp64pg6ye8j6h2gszs6k0y7u3p9hkstcy65s6"
    )
    created = await web.web_create_order(req, _req())
    await repository.update_status(created.order_id, OrderStatus.CONFIRMED)
    with pytest.raises(HTTPException) as e:        # too late once a payment confirmed
        await web.web_cancel_order(created.token)
    assert e.value.status_code == 409


async def test_web_cancel_bad_token(fresh_db, web_env):
    await web.ensure_web_service()
    with pytest.raises(HTTPException) as e:
        await web.web_cancel_order("999.deadbeef")
    assert e.value.status_code == 404


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


async def test_concurrency_cap_per_ip(fresh_db, web_env, monkeypatch):
    monkeypatch.setattr(settings, "web_max_concurrent_per_ip", 2)
    await web.ensure_web_service()
    req = web.WebOrderRequest(
        amount_usdt=5.0, destination_emc_address="em1qamp64pg6ye8j6h2gszs6k0y7u3p9hkstcy65s6"
    )
    for _ in range(2):
        await web.web_create_order(req, _req(ip="7.7.7.7"))
    with pytest.raises(HTTPException) as e:        # 3rd open order from the same IP
        await web.web_create_order(req, _req(ip="7.7.7.7"))
    assert e.value.status_code == 429
    await web.web_create_order(req, _req(ip="7.7.7.8"))   # a different IP is unaffected


async def test_pow_required_when_enabled(fresh_db, web_env, monkeypatch):
    monkeypatch.setattr(settings, "web_pow_enabled", True)
    monkeypatch.setattr(settings, "web_pow_bits", 8)
    await web.ensure_web_service()
    req = web.WebOrderRequest(   # no proof of work supplied
        amount_usdt=5.0, destination_emc_address="em1qamp64pg6ye8j6h2gszs6k0y7u3p9hkstcy65s6"
    )
    with pytest.raises(HTTPException) as e:
        await web.web_create_order(req, _req())
    assert e.value.status_code == 400


async def test_pow_accepts_valid_solution_and_blocks_replay(fresh_db, web_env, monkeypatch):
    monkeypatch.setattr(settings, "web_pow_enabled", True)
    monkeypatch.setattr(settings, "web_pow_bits", 8)
    await web.ensure_web_service()
    challenge, bits = web._new_pow_challenge()
    sol = _solve(challenge, bits)
    req = web.WebOrderRequest(
        amount_usdt=5.0, destination_emc_address="em1qamp64pg6ye8j6h2gszs6k0y7u3p9hkstcy65s6",
        pow_challenge=challenge, pow_solution=sol,
    )
    resp = await web.web_create_order(req, _req())
    assert resp.order_id > 0
    # the same challenge can't be spent twice (anti-replay)
    with pytest.raises(HTTPException) as e:
        await web.web_create_order(req, _req())
    assert e.value.status_code == 429


async def test_pow_rejects_wrong_solution(fresh_db, web_env, monkeypatch):
    monkeypatch.setattr(settings, "web_pow_enabled", True)
    monkeypatch.setattr(settings, "web_pow_bits", 12)
    await web.ensure_web_service()
    challenge, _ = web._new_pow_challenge()
    req = web.WebOrderRequest(
        amount_usdt=5.0, destination_emc_address="em1qamp64pg6ye8j6h2gszs6k0y7u3p9hkstcy65s6",
        pow_challenge=challenge, pow_solution="0",   # almost certainly not a valid 12-bit answer
    )
    with pytest.raises(HTTPException) as e:
        await web.web_create_order(req, _req())
    assert e.value.status_code == 400


async def test_client_ip_prefers_forwarded_for(fresh_db, web_env):
    assert web._client_ip(_req(ip="10.0.0.1", xff="203.0.113.7, 10.0.0.1")) == "203.0.113.7"
    assert web._client_ip(_req(ip="10.0.0.1")) == "10.0.0.1"


async def test_client_ip_prefers_cf_connecting_ip(fresh_db, web_env):
    # Behind Cloudflare, CF-Connecting-IP is authoritative and wins over XFF / peer.
    assert web._client_ip(
        _req(ip="10.0.0.1", xff="203.0.113.7", cf="198.51.100.9")
    ) == "198.51.100.9"


async def test_failed_order_does_not_burn_concurrency_slot(fresh_db, web_env, monkeypatch):
    # A creation that fails validation must not consume a per-IP slot: many bad
    # attempts from one IP should never trip the concurrency cap on their own.
    # (Raise the rate limit out of the way; this asserts only the concurrency slot.)
    monkeypatch.setattr(settings, "web_rate_per_min", 1000)
    await web.ensure_web_service()
    web._recent.clear()
    web._hits.clear()
    bad = web.WebOrderRequest(amount_usdt=5.0, destination_emc_address="not-an-emc-address")
    for _ in range(settings.web_max_concurrent_per_ip + 3):
        with pytest.raises(HTTPException) as e:
            await web.create_public_order(
                bad.amount_usdt, bad.destination_emc_address, _req(ip="9.9.9.9"),
                enforce_pow=False,
            )
        assert e.value.status_code == 400      # invalid address, NOT 429 "too many open orders"
    assert len(web._recent["9.9.9.9"]) == 0    # zero slots burned by failed attempts

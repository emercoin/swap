"""MCP exchanger surface: keyless agent tools mirroring the web channel, plus the
Streamable HTTP transport mounted at /mcp. Tools share the web service + buy_emc,
so a token minted here is the same opaque handle the web channel uses."""
import json
from types import SimpleNamespace

import pytest

from swap import mcp_app, repository, web
from swap.config import settings
from swap.models import OrderStatus

EMC_ADDR = "em1qamp64pg6ye8j6h2gszs6k0y7u3p9hkstcy65s6"


@pytest.fixture
def mcp_env(monkeypatch):
    monkeypatch.setattr(settings, "deposit_address", "TSharedDepositAddr")
    monkeypatch.setattr(settings, "emc_reserve_check", False)
    web._hits.clear()
    web._recent.clear()
    web._used_pow.clear()
    web._web_service = None


def _ctx(ip="5.6.7.8", xff=None):
    """A stand-in Context exposing just the Starlette request the tools read."""
    headers = {"x-forwarded-for": xff} if xff else {}
    request = SimpleNamespace(headers=headers, client=SimpleNamespace(host=ip))
    return SimpleNamespace(request_context=SimpleNamespace(request=request))


async def test_get_swap_config_reports_limits(fresh_db, mcp_env):
    cfg = await mcp_app.get_swap_config()
    assert (cfg.min_usdt, cfg.max_usdt, cfg.emc_per_usdt) == (5.0, 10.0, 10.0)


async def test_buy_emc_creates_keyless_order(fresh_db, mcp_env):
    resp = await mcp_app.buy_emc(_ctx(), amount_usdt=5.0, destination_emc_address=EMC_ADDR)
    assert resp.order_id > 0
    assert resp.deposit_address == "TSharedDepositAddr"
    assert resp.emc_amount == 50.0
    assert resp.status == OrderStatus.AWAITING_PAYMENT
    # backed by the first-party web service, no callback, token resolves to the order
    row = await repository.get_order(resp.order_id)
    assert row["service_id"] == web._web_service["id"]
    assert row["callback_url"] == ""
    assert web._order_id_from_token(resp.token) == resp.order_id


async def test_buy_emc_idempotency_key(fresh_db, mcp_env, monkeypatch):
    monkeypatch.setattr(settings, "web_max_concurrent_per_ip", 50)
    monkeypatch.setattr(settings, "web_rate_per_min", 50)
    OTHER = "Eeyqx7K8Yk9mWcCq4zZ6vUe2gJ3hNpRtAa"
    # same key + dest + amount → the SAME order (retry-safe, no duplicate)
    r1 = await mcp_app.buy_emc(_ctx(), amount_usdt=5.0, destination_emc_address=EMC_ADDR, idempotency_key="k1")
    r2 = await mcp_app.buy_emc(_ctx(), amount_usdt=5.0, destination_emc_address=EMC_ADDR, idempotency_key="k1")
    assert r1.order_id == r2.order_id
    # same key but a DIFFERENT destination → a new order (no cross-attribution leak)
    r3 = await mcp_app.buy_emc(_ctx(), amount_usdt=5.0, destination_emc_address=OTHER, idempotency_key="k1")
    assert r3.order_id != r1.order_id
    # no key → every call is a brand-new order
    r4 = await mcp_app.buy_emc(_ctx(), amount_usdt=5.0, destination_emc_address=EMC_ADDR)
    r5 = await mcp_app.buy_emc(_ctx(), amount_usdt=5.0, destination_emc_address=EMC_ADDR)
    assert r4.order_id != r5.order_id


async def test_buy_emc_rejects_bad_address(fresh_db, mcp_env):
    with pytest.raises(ValueError):
        await mcp_app.buy_emc(_ctx(), amount_usdt=5.0, destination_emc_address="nope!!")


async def test_buy_emc_rejects_out_of_range_amount(fresh_db, mcp_env):
    with pytest.raises(ValueError):
        await mcp_app.buy_emc(_ctx(), amount_usdt=1.0, destination_emc_address=EMC_ADDR)


async def test_get_order_status_by_token(fresh_db, mcp_env):
    created = await mcp_app.buy_emc(_ctx(), amount_usdt=5.0, destination_emc_address=EMC_ADDR)
    status = await mcp_app.get_order_status(token=created.token)
    assert status.status == OrderStatus.AWAITING_PAYMENT
    assert status.amount_usdt == created.amount_usdt
    assert status.deposit_address == "TSharedDepositAddr"


async def test_get_order_status_bad_token(fresh_db, mcp_env):
    with pytest.raises(ValueError):
        await mcp_app.get_order_status(token="999.deadbeef")


async def test_cancel_order(fresh_db, mcp_env):
    created = await mcp_app.buy_emc(_ctx(), amount_usdt=5.0, destination_emc_address=EMC_ADDR)
    resp = await mcp_app.cancel_order(token=created.token)
    assert resp.status == OrderStatus.EXPIRED
    # the freed tag no longer matches an incoming payment
    assert await repository.find_open_order_by_amount(created.amount_usdt) is None


async def test_per_ip_concurrency_cap_shared_with_web(fresh_db, mcp_env, monkeypatch):
    monkeypatch.setattr(settings, "web_max_concurrent_per_ip", 2)
    for _ in range(2):
        await mcp_app.buy_emc(_ctx(ip="7.7.7.7"), amount_usdt=5.0, destination_emc_address=EMC_ADDR)
    with pytest.raises(ValueError):          # 3rd open order from the same IP
        await mcp_app.buy_emc(_ctx(ip="7.7.7.7"), amount_usdt=5.0, destination_emc_address=EMC_ADDR)
    # a different IP is unaffected
    await mcp_app.buy_emc(_ctx(ip="7.7.7.8"), amount_usdt=5.0, destination_emc_address=EMC_ADDR)


# --- transport: Streamable HTTP mounted at /mcp ----------------------------

def _mcp_call(client, method, params=None, path="/mcp"):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    r = client.post(
        path,
        json=body,
        headers={"Accept": "application/json, text/event-stream"},
        follow_redirects=False,
    )
    assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text}"
    return r.json()




async def test_streamable_http_transport(fresh_db, mcp_env, monkeypatch):
    from fastapi.testclient import TestClient

    from swap import main
    monkeypatch.setattr(main, "RUN_WATCHER", False)   # no network/watcher in tests

    with TestClient(main.app) as client:
        # Canonical /mcp (no trailing slash) and /mcp/ both serve 200 directly — no
        # 307/405. MCP Inspector and other clients POST to /mcp without the slash.
        for path in ("/mcp", "/mcp/"):
            ok = _mcp_call(client, "tools/list", path=path)
            assert "buy_emc" in {t["name"] for t in ok["result"]["tools"]}

        listed = _mcp_call(client, "tools/list")
        tools = {t["name"]: t for t in listed["result"]["tools"]}
        assert {"get_swap_config", "buy_emc", "get_order_status", "cancel_order"} <= set(tools)
        # TDQS metadata is emitted: titles, behavior annotations, output schemas.
        assert tools["buy_emc"]["title"]
        assert tools["buy_emc"]["annotations"]["openWorldHint"] is True
        assert tools["get_swap_config"]["annotations"]["readOnlyHint"] is True
        assert tools["cancel_order"]["annotations"]["destructiveHint"] is True
        assert tools["buy_emc"]["outputSchema"]["properties"]["deposit_address"]

        called = _mcp_call(client, "tools/call", {
            "name": "buy_emc",
            "arguments": {"amount_usdt": 5.0, "destination_emc_address": EMC_ADDR},
        })
        assert called["result"]["isError"] is False
        payload = json.loads(called["result"]["content"][0]["text"])
        assert payload["deposit_address"] == "TSharedDepositAddr"
        assert payload["emc_amount"] == 50.0
        assert web._order_id_from_token(payload["token"]) == payload["order_id"]

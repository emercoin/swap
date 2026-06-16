"""Public stats digest: DB aggregates (counts / delivered / windows) and the
stats service — happy snapshot, upstream-outage degradation, and the TTL cache."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from swap import repository
from swap.config import settings
from swap.db import get_conn
from swap.services import stats

_TS = "%Y-%m-%dT%H:%M:%SZ"


def _ago(**kw) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kw)).strftime(_TS)


async def _seed(ref, emc, status, *, txid=None, created=None):
    """Insert one order with full control over status/txid/created_at, bypassing the
    state machine (fixture data, not a real transition)."""
    conn = await get_conn()
    sid = await repository.create_service(f"svc-{ref}", f"key-{ref}", "secret")
    await conn.execute(
        """INSERT INTO orders
               (service_id, ref, amount_usdt, emc_amount, destination_emc,
                callback_url, status, emc_txid, expires_at, created_at)
           VALUES (?, ?, ?, ?, 'Edst', '', ?, ?, '2099-01-01T00:00:00Z', ?)""",
        (sid, ref, emc / 10, emc, status, txid, created or _ago(seconds=1)),
    )
    await conn.commit()


@pytest.fixture(autouse=True)
def _clear_cache():
    stats.reset_cache()
    yield
    stats.reset_cache()


# --- repository aggregates -------------------------------------------------

async def test_order_counts_and_delivered_total(fresh_db):
    await _seed("n1", 50, "notified", txid="tx1")
    await _seed("d1", 30, "emc_delivered", txid="tx2")
    await _seed("c1", 20, "confirmed")
    await _seed("a1", 10, "awaiting_payment")

    counts = await repository.order_counts()
    assert counts["total"] == 4
    assert counts["notified"] == 1 and counts["emc_delivered"] == 1
    assert counts["confirmed"] == 1 and counts["awaiting_payment"] == 1

    # Only orders with a delivery txid count toward EMC actually sent.
    assert await repository.delivered_emc_total() == 80.0


async def test_activity_windows(fresh_db):
    await _seed("now", 50, "notified", txid="t", created=_ago(hours=2))
    await _seed("mid", 40, "emc_delivered", txid="t", created=_ago(days=3))
    await _seed("old", 10, "expired", created=_ago(days=10))

    last_24h = await repository.activity_since(_ago(days=1))
    assert last_24h == {"created": 1, "delivered": 1, "delivered_emc": 50.0}

    last_7d = await repository.activity_since(_ago(days=7))
    assert last_7d["created"] == 2            # now + mid, not the 10-day-old one
    assert last_7d["delivered"] == 2
    assert last_7d["delivered_emc"] == 90.0   # 50 + 40 delivered


# --- stats service ---------------------------------------------------------

class FakeAdapter:
    calls = 0
    async def balance(self):
        FakeAdapter.calls += 1
        return {"balance": 500.0, "unconfirmed": 0}
    async def aclose(self): ...


class FakeTron:
    calls = 0
    async def usdt_balance_of(self, address):
        FakeTron.calls += 1
        return 123.456789
    async def aclose(self): ...


class DownAdapter:
    async def balance(self):
        raise httpx.ConnectError("adapter unreachable")
    async def aclose(self): ...


class DownTron:
    async def usdt_balance_of(self, address):
        raise RuntimeError("balanceOf returned no result")
    async def aclose(self): ...


@pytest.fixture
def stats_env(monkeypatch):
    monkeypatch.setattr(settings, "deposit_address", "TDeposit")
    monkeypatch.setattr(settings, "emc_reserve_buffer", 0.0)
    monkeypatch.setattr(settings, "stats_cache_ttl_seconds", 60)
    FakeAdapter.calls = FakeTron.calls = 0


async def test_get_stats_happy(fresh_db, stats_env, monkeypatch):
    monkeypatch.setattr(stats, "AdapterClient", FakeAdapter)
    monkeypatch.setattr(stats, "TronGridClient", FakeTron)
    await _seed("c1", 20, "confirmed")             # 20 EMC outstanding
    await _seed("n1", 50, "notified", txid="tx")

    s = await stats.get_stats()
    assert s["balances"]["usdt_deposit"] == pytest.approx(123.456789)
    assert s["balances"]["deposit_address"] == "TDeposit"
    r = s["balances"]["emc_reserve"]
    assert r["balance"] == 500.0
    assert r["outstanding"] == 20.0 and r["available"] == 480.0
    assert s["orders"]["total"] == 2
    assert s["orders"]["delivered"] == 1           # notified
    assert s["orders"]["delivered_emc"] == 50.0


async def test_get_stats_degrades_on_upstream_outage(fresh_db, stats_env, monkeypatch):
    monkeypatch.setattr(stats, "AdapterClient", DownAdapter)
    monkeypatch.setattr(stats, "TronGridClient", DownTron)
    await _seed("n1", 50, "notified", txid="tx")

    s = await stats.get_stats()
    assert s["balances"]["emc_reserve"] is None    # adapter down → null, not a 500
    assert s["balances"]["usdt_deposit"] is None    # TronGrid down → null
    assert s["orders"]["total"] == 1                # DB metrics always render
    assert s["orders"]["delivered_emc"] == 50.0


async def test_get_stats_is_cached(fresh_db, stats_env, monkeypatch):
    monkeypatch.setattr(stats, "AdapterClient", FakeAdapter)
    monkeypatch.setattr(stats, "TronGridClient", FakeTron)
    await stats.get_stats()
    await stats.get_stats()
    assert FakeAdapter.calls == 1                   # second hit served from cache
    assert FakeTron.calls == 1

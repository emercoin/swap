"""Reserve monitor: headroom math mirrors the pre-flight, the low-reserve alert
fires/clears with hysteresis, the interval throttles checks, and an adapter
outage degrades quietly."""
import logging

import httpx
import pytest

from swap import repository
from swap.config import settings
from swap.models import OrderStatus
from swap.services import monitor
from swap.services.monitor import ReserveMonitor


@pytest.fixture
def reserve_env(monkeypatch):
    monkeypatch.setattr(settings, "emc_reserve_buffer", 0.0)
    monkeypatch.setattr(settings, "reserve_low_watermark", 300.0)
    monkeypatch.setattr(settings, "reserve_monitor_interval_minutes", 15)


class FakeAdapter:
    def __init__(self, balance: float):
        self.balance_value = balance
        self.calls = 0

    async def balance(self):
        self.calls += 1
        return {"balance": self.balance_value, "unconfirmed": 0}


class DownAdapter:
    async def balance(self):
        raise httpx.ConnectError("adapter unreachable")


async def _confirmed_order(emc_amount: float, ref: str) -> int:
    """A paid, undelivered order — counts toward outstanding obligations."""
    sid = await repository.create_service(f"svc-{ref}", f"k-{ref}", "secret")
    oid = await repository.insert_order(
        service_id=sid, ref=ref, amount_usdt=emc_amount / 10, emc_amount=emc_amount,
        destination_emc="EMCdest", callback_url="",
        expires_at="2099-01-01T00:00:00Z",
    )
    await repository.update_status(oid, OrderStatus.CONFIRMED)
    return oid


async def test_read_reserve_matches_preflight_math(fresh_db, reserve_env):
    await _confirmed_order(50.0, "a")
    await _confirmed_order(100.0, "b")
    s = await monitor.read_reserve(FakeAdapter(balance=500.0))
    assert s.balance == 500.0
    assert s.outstanding == 150.0          # 50 + 100 owed by confirmed, undelivered
    assert s.available == 350.0            # 500 − 150 − 0 buffer
    assert s.low is False                  # 350 ≥ 300 watermark


async def test_buffer_is_subtracted(fresh_db, reserve_env, monkeypatch):
    monkeypatch.setattr(settings, "emc_reserve_buffer", 100.0)
    s = await monitor.read_reserve(FakeAdapter(balance=500.0))
    assert s.available == 400.0            # 500 − 0 outstanding − 100 buffer


async def test_low_alert_fires_and_recovers(fresh_db, reserve_env, caplog):
    mon = ReserveMonitor()
    with caplog.at_level(logging.WARNING, logger="swap.monitor"):
        low = await mon.check(FakeAdapter(balance=250.0))   # 250 < 300
    assert low.low is True
    assert any("LOW EMC RESERVE" in r.message for r in caplog.records)
    assert mon._was_low is True

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="swap.monitor"):
        ok = await mon.check(FakeAdapter(balance=500.0))    # back above watermark
    assert ok.low is False
    assert any("recovered" in r.message for r in caplog.records)
    assert mon._was_low is False


async def test_run_if_due_throttles_to_interval(fresh_db, reserve_env):
    mon = ReserveMonitor()
    adapter = FakeAdapter(balance=500.0)
    first = await mon.run_if_due(adapter)        # 0 due → runs immediately
    assert first is not None
    assert adapter.calls == 1
    second = await mon.run_if_due(adapter)        # within the interval → skipped
    assert second is None
    assert adapter.calls == 1                      # adapter not queried again


async def test_adapter_outage_is_quiet(fresh_db, reserve_env, caplog):
    mon = ReserveMonitor()
    with caplog.at_level(logging.WARNING, logger="swap.monitor"):
        res = await mon.run_if_due(DownAdapter())
    assert res is None                             # degrades, no crash
    assert any("balance unavailable" in r.message for r in caplog.records)

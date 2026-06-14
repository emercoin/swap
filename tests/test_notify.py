"""Callback wiring + retry behaviour driven through watcher._notify_delivered."""
import httpx
import pytest

from swap import repository
from swap.config import settings
from swap.models import OrderStatus
from swap.services import callback, watcher


async def _delivered_order(ref: str = "inv-1", url: str = "http://svc/cb") -> int:
    """Create a service + an order parked in emc_delivered with an emc_txid."""
    sid = await repository.create_service("svc", "swk_test", "topsecret")
    oid = await repository.insert_order(
        service_id=sid, ref=ref, amount_usdt=5.0, emc_amount=50.0,
        destination_emc="EMCdest", callback_url=url,
        expires_at="2999-01-01T00:00:00Z",
    )
    await repository.update_status(oid, OrderStatus.CONFIRMED)  # inserted awaiting → confirmed
    await repository.set_emc_txid(oid, "emc-tx-abc")
    await repository.update_status(oid, OrderStatus.EMC_DELIVERED)
    return oid


@pytest.fixture
def capture_posts(monkeypatch):
    """Replace the real HTTP POST; record signed args and return a chosen status."""
    calls = []
    status_box = {"code": 200, "raise": False}

    async def fake_post(*, url, body, ts, secret):
        calls.append({"url": url, "body": body, "ts": ts, "secret": secret})
        if status_box["raise"]:
            raise httpx.ConnectError("boom")
        return status_box["code"]

    monkeypatch.setattr(callback, "post_callback", fake_post)
    return calls, status_box


async def test_success_marks_notified(fresh_db, capture_posts):
    calls, box = capture_posts
    box["code"] = 200
    oid = await _delivered_order()

    await watcher._notify_delivered()

    order = await repository.get_order(oid)
    cb = await repository.get_callback_for_order(oid)
    assert order["status"] == OrderStatus.NOTIFIED
    assert cb["delivered"] == 1 and cb["attempts"] == 1 and cb["last_status"] == 200
    # body was signed with the service's secret and carries the order facts
    assert callback.verify(calls[0]["body"], "topsecret", callback.sign(calls[0]["body"], "topsecret"))
    assert '"emc_txid":"emc-tx-abc"' in calls[0]["body"]


async def test_failure_schedules_retry_and_stays_delivered(fresh_db, capture_posts):
    _, box = capture_posts
    box["code"] = 500
    oid = await _delivered_order()

    await watcher._notify_delivered()

    order = await repository.get_order(oid)
    cb = await repository.get_callback_for_order(oid)
    assert order["status"] == OrderStatus.EMC_DELIVERED  # not notified
    assert cb["delivered"] == 0 and cb["attempts"] == 1
    assert cb["last_status"] == 500 and cb["next_retry_at"] is not None


async def test_network_error_counts_as_failure(fresh_db, capture_posts):
    _, box = capture_posts
    box["raise"] = True
    oid = await _delivered_order()

    await watcher._notify_delivered()

    cb = await repository.get_callback_for_order(oid)
    assert cb["attempts"] == 1 and cb["last_status"] == 0 and cb["delivered"] == 0


async def test_web_order_settles_without_callback(fresh_db, capture_posts):
    """A public/web order (empty callback_url) goes emc_delivered → notified with
    no callback enqueued — delivery is its terminal step."""
    calls, _ = capture_posts
    oid = await _delivered_order(url="")          # no callback target

    await watcher._notify_delivered()

    order = await repository.get_order(oid)
    assert order["status"] == OrderStatus.NOTIFIED
    assert await repository.get_callback_for_order(oid) is None   # nothing enqueued
    assert calls == []                                            # nothing posted


async def test_not_due_is_skipped(fresh_db, capture_posts):
    calls, box = capture_posts
    box["code"] = 500
    await _delivered_order()

    await watcher._notify_delivered()   # attempt #1 → fails, sets future next_retry_at
    await watcher._notify_delivered()   # not due yet → must NOT POST again

    assert len(calls) == 1


async def test_exhausted_retries_stop(fresh_db, capture_posts, monkeypatch):
    calls, box = capture_posts
    box["code"] = 500
    monkeypatch.setattr(settings, "callback_max_retries", 1)
    oid = await _delivered_order()

    await watcher._notify_delivered()                       # attempt #1 (attempts→1)
    # force it "due" again, but attempts already at the cap
    cb = await repository.get_callback_for_order(oid)
    await repository.record_callback_failure(cb["id"], last_status=500, next_retry_at=None)
    await watcher._notify_delivered()                       # capped → no further POST

    assert len(calls) == 1

"""Signed callback notifier + retries.

The signature is what stops a forged 'paid' from buying a free service, so it is
mandatory. Scheme (decided §8): **HMAC-SHA256** over the exact JSON body using
the calling service's `callback_secret`. We send:

    POST {callback_url}
    Headers:
      X-Swap-Signature: sha256=<hex hmac of the raw body>
      X-Swap-Timestamp: <unix seconds>   (bound into the signed body to stop replay)
    Body (canonical JSON): {ref, order_id, status, emc_txid, ts}

The service recomputes the HMAC with its shared secret and compares (constant
time). `notified` is only reached once the callback is acknowledged (2xx);
otherwise it is retried with backoff, and the service can fall back to polling
GET /order/{id}.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

import httpx

log = logging.getLogger("swap.callback")


def canonical_body(*, ref: str, order_id: int, status: str, emc_txid: str | None) -> tuple[str, str]:
    """Return (raw_json_body, unix_ts). Body is compact, key-sorted JSON so the
    bytes the service verifies are exactly the bytes we signed."""
    ts = str(int(time.time()))
    payload = {
        "ref": ref,
        "order_id": order_id,
        "status": status,
        "emc_txid": emc_txid,
        "ts": ts,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True), ts


def sign(body: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify(body: str, secret: str, signature: str) -> bool:
    """Reference verifier (for the consumer service / tests)."""
    return hmac.compare_digest(sign(body, secret), signature)


async def post_callback(*, url: str, body: str, ts: str, secret: str) -> int:
    """POST the signed callback once. Returns the HTTP status code."""
    headers = {
        "Content-Type": "application/json",
        "X-Swap-Signature": sign(body, secret),
        "X-Swap-Timestamp": ts,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, content=body, headers=headers)
    log.info("callback POST %s -> %s", url, resp.status_code)
    return resp.status_code


# Business status reported to the service: the user paid and EMC was delivered.
PAID = "paid"


async def send_for_order(*, order_row, secret: str) -> tuple[bool, int]:
    """Build, sign and POST the callback for a delivered order.

    Re-signs with a fresh timestamp on every call so retries are not rejected as
    stale/replayed. Returns (delivered, http_status); a network error is
    reported as (False, 0) so the caller schedules a retry.
    """
    body, ts = canonical_body(
        ref=order_row["ref"],
        order_id=order_row["id"],
        status=PAID,
        emc_txid=order_row["emc_txid"],
    )
    try:
        code = await post_callback(
            url=order_row["callback_url"], body=body, ts=ts, secret=secret
        )
    except httpx.HTTPError as exc:
        log.warning("callback to %s failed: %s", order_row["callback_url"], exc)
        return False, 0
    return 200 <= code < 300, code


def backoff_seconds(attempt: int) -> int:
    """Exponential backoff capped at 1h: 30s, 60s, 120s, ... (attempt is 1-based)."""
    return min(30 * (2 ** (attempt - 1)), 3600)

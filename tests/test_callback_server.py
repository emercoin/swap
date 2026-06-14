"""The E2E callback receiver verifies the swap HMAC signature."""
from fastapi.testclient import TestClient

from scripts.testnet.callback_server import build_app
from swap.services import callback

SECRET = "topsecret"


def _client():
    return TestClient(build_app(SECRET))


def test_accepts_valid_signature():
    body, ts = callback.canonical_body(ref="r", order_id=1, status="paid", emc_txid="tx")
    sig = callback.sign(body, SECRET)
    r = _client().post("/cb", content=body,
                       headers={"X-Swap-Signature": sig, "X-Swap-Timestamp": ts})
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_rejects_forged_signature():
    body, ts = callback.canonical_body(ref="r", order_id=1, status="paid", emc_txid="tx")
    r = _client().post("/cb", content=body,
                       headers={"X-Swap-Signature": "sha256=deadbeef", "X-Swap-Timestamp": ts})
    assert r.status_code == 401


def test_rejects_tampered_body():
    body, ts = callback.canonical_body(ref="r", order_id=1, status="paid", emc_txid="tx")
    sig = callback.sign(body, SECRET)               # signature for the original body
    tampered = body.replace('"order_id":1', '"order_id":2')
    r = _client().post("/cb", content=tampered,
                       headers={"X-Swap-Signature": sig, "X-Swap-Timestamp": ts})
    assert r.status_code == 401

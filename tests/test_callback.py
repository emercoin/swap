from swap.services import callback


def test_sign_verify_roundtrip():
    body, ts = callback.canonical_body(
        ref="inv-1", order_id=42, status="paid", emc_txid="abc"
    )
    secret = "shared-secret"
    sig = callback.sign(body, secret)
    assert sig.startswith("sha256=")
    assert callback.verify(body, secret, sig)


def test_verify_rejects_wrong_secret():
    body, _ = callback.canonical_body(ref="r", order_id=1, status="paid", emc_txid=None)
    sig = callback.sign(body, "right")
    assert not callback.verify(body, "wrong", sig)


def test_verify_rejects_tampered_body():
    body, _ = callback.canonical_body(ref="r", order_id=1, status="paid", emc_txid=None)
    sig = callback.sign(body, "s")
    assert not callback.verify(body.replace('"order_id":1', '"order_id":2'), "s", sig)


def test_backoff_is_capped():
    assert callback.backoff_seconds(1) == 30
    assert callback.backoff_seconds(2) == 60
    assert callback.backoff_seconds(100) == 3600

"""End-to-end on Nile: create an order → pay it → watch it reach `confirmed`.

    uv run python -m scripts.testnet.e2e --api-key <swk_...> [--amount 5] \
        [--api http://localhost:8002] [--dest <emc_addr>]

Prereqs:
  * swap running with the Nile env: SWAP_TRONGRID_URL=https://nile.trongrid.io,
    SWAP_USDT_CONTRACT=<deployed token>, SWAP_TRON_MNEMONIC=<your testnet mnemonic>
  * a registered service (scripts/register_service.py) → its api_key
  * payer funded with test TRX and holding the deployed token

This validates the watcher path up to `confirmed` (USDT detected + matched).
Reaching `emc_delivered`/`notified` additionally needs the emercoin adapter with
an EMC reserve — out of scope for the TRON-watcher test.
"""
from __future__ import annotations

import argparse
import time

import httpx

from . import _common as c
from .pay import pay


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--api", default="http://localhost:8002")
    ap.add_argument("--amount", type=float, default=5.0)
    ap.add_argument("--dest", default="EMCtestDestinationAddress")
    ap.add_argument("--callback-url", default=None,
                    help="if set, used as the order callback_url and polling "
                         "continues until 'notified' (run callback_server.py there)")
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()

    headers = {"X-API-Key": args.api_key}
    ref = f"e2e-{int(time.time())}"
    callback_url = args.callback_url or "http://localhost:9/cb"
    expect_notified = args.callback_url is not None

    with httpx.Client(base_url=args.api, headers=headers, timeout=30) as api:
        r = api.post("/buy_emc", json={
            "amount_usdt": args.amount,
            "destination_emc_address": args.dest,
            "callback_url": callback_url,
            "ref": ref,
        })
        r.raise_for_status()
        order = r.json()
        oid, deposit = order["order_id"], order["deposit_address"]
        # swap returns the EXACT tagged amount to pay — match is by amount now.
        exact = order["amount_usdt"]
        print(f"order {oid}: pay EXACTLY {exact} USDT → {deposit}")

        pay(deposit, exact)  # send the exact tagged amount from the payer

        print("polling order status (watcher needs ~confirmations + poll tick) ...")
        deadline = time.time() + args.timeout
        last = None
        # With a real callback receiver we want the full loop to `notified`;
        # otherwise stop once EMC is delivered. Branch failures always stop.
        success = {"notified"} if expect_notified else {"emc_delivered", "notified"}
        failures = {"deliver_failed", "underpaid", "overpaid", "aml_hold"}
        while time.time() < deadline:
            s = api.get(f"/order/{oid}").json()["status"]
            if s != last:
                print(f"  status: {s}")
                last = s
            if s in success:
                print(f"DONE: reached '{s}'")
                return
            if s in failures:
                note = " (delivery failed — expected without adapter)" if s == "deliver_failed" else ""
                print(f"STOP: reached '{s}'{note}")
                return
            time.sleep(5)
        print("TIMEOUT: order did not reach a terminal state in time")


if __name__ == "__main__":
    main()

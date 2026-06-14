"""Send test USDT from the payer to a deposit address (simulates the user/agent).

    uv run python -m scripts.testnet.pay <deposit_address> <amount_usdt>

Uses the token deployed by deploy_token.py (from state.json).
"""
from __future__ import annotations

import sys

from . import _common as c


def pay(to_address: str, amount_usdt: float) -> str:
    state = c.load_state()
    token = state.get("token")
    if not token:
        sys.exit("no test token deployed yet — run scripts.testnet.deploy_token")

    priv = c.load_or_create_payer()
    payer = c.payer_address(priv)
    cli = c.client()
    contract = cli.get_contract(token)

    units = c.to_units(amount_usdt)
    ret = (
        contract.functions.transfer(to_address, units)
        .with_owner(payer)
        .fee_limit(100_000_000)
        .build()
        .sign(priv)
        .broadcast()
    )
    ret.wait()
    print(f"sent {amount_usdt} test-USDT → {to_address}  (txid {ret.txid})")
    return ret.txid


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit("usage: python -m scripts.testnet.pay <deposit_address> <amount_usdt>")
    pay(sys.argv[1], float(sys.argv[2]))


if __name__ == "__main__":
    main()

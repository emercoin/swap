"""Create / show the Nile payer wallet and its balances.

    uv run python -m scripts.testnet.wallet

First run mints a throwaway key (payer.key). Then fund the printed address with
test TRX from the faucet (needed for gas to deploy the token and send transfers).
"""
from __future__ import annotations

from . import _common as c


def main() -> None:
    priv = c.load_or_create_payer()
    addr = c.payer_address(priv)
    cli = c.client()
    try:
        trx = cli.get_account_balance(addr)  # TRX, float
    except Exception:
        trx = 0  # unactivated account (no funds yet)

    state = c.load_state()
    print(f"payer address : {addr}")
    print(f"TRX balance   : {trx}")
    if state.get("token"):
        print(f"test token    : {state['token']}")
    print(f"\nFund this address with test TRX: {c.FAUCET}")
    print("Then: python -m scripts.testnet.deploy_token")


if __name__ == "__main__":
    main()

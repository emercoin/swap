"""Compile and deploy the test TRC20 to Nile; mint the whole supply to the payer.

    uv run python -m scripts.testnet.deploy_token

Prints the contract address and saves it to state.json. Put it in your .env as
SWAP_USDT_CONTRACT so the swap watcher matches transfers of *this* token.
Requires the payer to hold a little test TRX (see wallet.py / faucet).
"""
from __future__ import annotations

from pathlib import Path

import solcx
from tronpy.contract import Contract
from tronpy.keys import to_base58check_address

from . import _common as c

SOLC_VERSION = "0.8.23"
SOURCE = Path(__file__).with_name("TestUSDT.sol")
INITIAL_SUPPLY = c.to_units(1_000_000)  # 1,000,000 test USDT to the payer


def _compile() -> tuple[str, list]:
    solcx.install_solc(SOLC_VERSION)
    out = solcx.compile_files(
        [str(SOURCE)], output_values=["abi", "bin"], solc_version=SOLC_VERSION
    )
    key = next(k for k in out if k.endswith(":TestUSDT"))
    return out[key]["bin"], out[key]["abi"]


def main() -> None:
    priv = c.load_or_create_payer()
    payer = c.payer_address(priv)
    cli = c.client()
    bytecode, abi = _compile()

    cntr = Contract(
        name="TestUSDT", bytecode=bytecode, abi=abi, client=cli,
        origin_address=payer, owner_address=payer,
    )
    # Append ABI-encoded constructor args to the creation bytecode.
    cntr.bytecode = cntr.bytecode + cntr.constructor.encode_parameter(INITIAL_SUPPLY)

    print(f"deploying TestUSDT from {payer} ...")
    ret = cntr.deploy().fee_limit(2_000_000_000).build().sign(priv).broadcast()
    info = ret.wait()
    token = to_base58check_address(info["contract_address"])
    c.save_state(token=token)

    print(f"deployed: {token}  (txid {ret.txid})")
    print(f"\nAdd to .env:\n  SWAP_USDT_CONTRACT={token}")


if __name__ == "__main__":
    main()

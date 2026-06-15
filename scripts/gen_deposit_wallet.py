"""Generate a fresh TRON deposit wallet OFFLINE for the mainnet cutover.

    uv run python -m scripts.gen_deposit_wallet [--index N]

Run this on an offline machine. It prints a brand-new BIP39 mnemonic and the
matching TRON deposit address (BIP44 m/44'/195'/0'/0/{index}, 195 = TRON).

Put ONLY the printed address into SWAP_DEPOSIT_ADDRESS on the server. Keep the
mnemonic offline: it is the spend key for every USDT collected on that address,
and the watcher never needs it (it only reads TRON). Nothing is written to disk.
"""
from __future__ import annotations

import argparse

from bip_utils import (
    Bip39MnemonicGenerator,
    Bip39SeedGenerator,
    Bip39WordsNum,
    Bip44,
    Bip44Changes,
    Bip44Coins,
)


def generate(index: int) -> tuple[str, str]:
    """Return (mnemonic, base58 TRON address) for a new wallet at `index`."""
    mnemonic = str(Bip39MnemonicGenerator().FromWordsNumber(Bip39WordsNum.WORDS_NUM_24))
    seed = Bip39SeedGenerator(mnemonic).Generate()
    account = (
        Bip44.FromSeed(seed, Bip44Coins.TRON)
        .Purpose()
        .Coin()
        .Account(0)
        .Change(Bip44Changes.CHAIN_EXT)
        .AddressIndex(index)
    )
    return mnemonic, account.PublicKey().ToAddress()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an offline TRON deposit wallet.")
    parser.add_argument("--index", type=int, default=0, help="BIP44 address index (default 0)")
    args = parser.parse_args()

    mnemonic, address = generate(args.index)
    print("Run this OFFLINE. Store the mnemonic on paper / in a password manager;")
    print("never put it on the server or in git. Only the address goes on the host.\n")
    print(f"mnemonic (24 words):\n  {mnemonic}\n")
    print(f"SWAP_DEPOSIT_ADDRESS={address}   # BIP44 index {args.index}")


if __name__ == "__main__":
    main()

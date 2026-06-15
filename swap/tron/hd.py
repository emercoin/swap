"""HD derivation of per-order TRON deposit addresses.

One address per order, index = order_id → unique, deterministic, recoverable
from the mnemonic alone. BIP44 path m/44'/195'/0'/0/{index} (195 = TRON).

The mnemonic (`SWAP_TRON_MNEMONIC`) is the master secret for all deposit funds;
keep it out of git and ideally off the watcher host. Spending swept USDT/TRX
needs the matching private keys, also derivable here via `derive_private_key`.
"""
from __future__ import annotations

from functools import lru_cache

from ..config import settings

# TRON account: m/44'/195'/0'/0/{index}
_ACCOUNT = 0
_CHANGE = 0  # external chain


@lru_cache
def _account_ctx():
    # Imported lazily so the package imports without bip_utils present.
    from bip_utils import Bip39SeedGenerator, Bip44, Bip44Changes, Bip44Coins

    if not settings.tron_mnemonic:
        raise RuntimeError("SWAP_TRON_MNEMONIC is not set")
    seed = Bip39SeedGenerator(settings.tron_mnemonic).Generate()
    return (
        Bip44.FromSeed(seed, Bip44Coins.TRON)
        .Purpose()
        .Coin()
        .Account(_ACCOUNT)
        .Change(Bip44Changes.CHAIN_EXT),
    )[0]


def derive_deposit_address(index: int) -> str:
    """Base58 TRON address (T...) for the given order index."""
    return _account_ctx().AddressIndex(index).PublicKey().ToAddress()


def derive_private_key(index: int) -> str:
    """Raw hex private key for the deposit address — needed to sweep its funds."""
    return _account_ctx().AddressIndex(index).PrivateKey().Raw().ToHex()

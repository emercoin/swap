"""Shared helpers for the Nile testnet scripts.

A throwaway payer key lives in `payer.key` (gitignored); the deployed test-token
address is remembered in `state.json` so `pay.py` / `e2e.py` need no copy-paste.
None of this touches the swap service's own HD wallet — it is the *payer* side.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from tronpy import Tron
from tronpy.keys import PrivateKey

NILE_URL = os.environ.get("SWAP_TRONGRID_URL", "https://nile.trongrid.io")
FAUCET = "https://nileex.io/join/getJoinPage"

_HERE = Path(__file__).parent
KEY_FILE = _HERE / "payer.key"
STATE_FILE = _HERE / "state.json"
DECIMALS = 6


def client() -> Tron:
    return Tron(network="nile")


def load_or_create_payer() -> PrivateKey:
    if KEY_FILE.exists():
        return PrivateKey(bytes.fromhex(KEY_FILE.read_text().strip()))
    priv = PrivateKey.random()
    KEY_FILE.write_text(priv.hex())
    KEY_FILE.chmod(0o600)
    return priv


def payer_address(priv: PrivateKey) -> str:
    return priv.public_key.to_base58check_address()


def to_units(human: float) -> int:
    return int(round(human * 10 ** DECIMALS))


def from_units(units: int) -> float:
    return units / 10 ** DECIMALS


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(**kw) -> None:
    state = load_state()
    state.update(kw)
    STATE_FILE.write_text(json.dumps(state, indent=2))

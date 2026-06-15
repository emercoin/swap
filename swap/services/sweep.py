"""Sweep collected USDT from deposit addresses into consolidation.

After EMC is delivered, the USDT sitting on the per-order deposit address is
swept to `SWAP_SWEEP_ADDRESS` (cold/consolidation). TRC20 transfers cost TRON
energy/bandwidth, so each deposit address must first be funded with a little TRX
for gas — track and top this up.

Not yet implemented: signing a TRC20 transfer with the deposit address's derived
key needs a TRON tx builder (e.g. tronpy) and must be validated in a test
environment before it touches real funds.
"""
from __future__ import annotations

import logging

log = logging.getLogger("swap.sweep")


async def sweep_order(order_id: int) -> str:
    """Build, sign and broadcast the USDT sweep for one order. Returns tron_txid.

    Outline:
      key = tron.hd.derive_private_key(order_id)
      ensure the deposit address holds enough TRX for gas (else top up first)
      build TRC20 transfer(full balance) -> settings.sweep_address, sign, broadcast
      record in `sweeps` (pending → sent → confirmed)
    """
    raise NotImplementedError("TRON tx signing — validate in a test environment before real funds")

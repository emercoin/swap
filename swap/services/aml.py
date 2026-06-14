"""AML screening of the incoming sender address.

This is NOT a formality: a 'dirty' USDT deposit can be frozen by Tether right on
our address, so screening protects working capital (§3). Free sources:
  - OFAC SDN list (sanctioned addresses)
  - public Tether freeze blacklist

A hit → order goes to `aml_hold` for manual review; EMC is never delivered.
"""
from __future__ import annotations

from dataclasses import dataclass

# In-memory blacklist; loaded at startup and refreshed periodically.
# Maps a normalised TRON address -> source tag ("ofac" | "tether" | ...).
_blacklist: dict[str, str] = {}


@dataclass
class AmlResult:
    clear: bool
    source: str | None = None  # which list matched, when not clear


def screen(address: str) -> AmlResult:
    """Pure, synchronous lookup against the loaded blacklist."""
    src = _blacklist.get(address)
    return AmlResult(clear=src is None, source=src)


async def load_blacklists() -> int:
    """Populate `_blacklist` from OFAC SDN + Tether freeze lists.

    TODO(sandbox): fetch and parse:
      - OFAC SDN (sanctions list; filter Digital Currency Address - TRX entries)
      - a public Tether-frozen-addresses feed
    Verify the parsing in the sandbox, then schedule a periodic refresh.
    Returns the number of addresses loaded.
    """
    raise NotImplementedError("load OFAC + Tether lists; verify parsing in sandbox")

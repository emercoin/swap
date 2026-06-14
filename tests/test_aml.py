"""AML: OFAC list parsing + the OFAC/Tether screen logic."""
import pytest

from swap.services import aml

OFAC_SAMPLE = """\
TASWbk6X1wiTku5TMmMQYqYFvshVEtfJy8
TAYhjpL8pPs8T84FSM329nffQpc6jD8GBM
# a comment / junk line
0xdeadbeef
TAoLw5yD5XUoHWeBZRSZ1ExK9HMv2CiPvP
"""


class FakeTron:
    def __init__(self, frozen=()):
        self._frozen = set(frozen)
        self.calls = 0

    async def usdt_is_blacklisted(self, address):
        self.calls += 1
        return address in self._frozen


def test_parse_ofac_keeps_only_tron_addresses():
    addrs = aml.parse_ofac(OFAC_SAMPLE)
    assert len(addrs) == 3
    assert all(a.startswith("T") and len(a) == 34 for a in addrs)
    assert "0xdeadbeef" not in addrs


def test_screen_in_memory(monkeypatch):
    monkeypatch.setattr(aml, "_blacklist", {"Tbad": "ofac"})
    assert not aml.screen("Tbad").clear
    assert aml.screen("Tbad").source == "ofac"
    assert aml.screen("Tgood").clear


async def test_screen_full_ofac_short_circuits_tether(monkeypatch):
    monkeypatch.setattr(aml, "_blacklist", {"Tbad": "ofac"})
    tron = FakeTron()
    res = await aml.screen_full("Tbad", tron)
    assert not res.clear and res.source == "ofac"
    assert tron.calls == 0          # OFAC hit → no live Tether call needed


async def test_screen_full_live_tether_hit(monkeypatch):
    monkeypatch.setattr(aml, "_blacklist", {})
    res = await aml.screen_full("Tfrozen", FakeTron(frozen={"Tfrozen"}))
    assert not res.clear and res.source == "tether"


async def test_screen_full_tether_error_is_fail_open(monkeypatch):
    monkeypatch.setattr(aml, "_blacklist", {})

    class Boom:
        async def usdt_is_blacklisted(self, address):
            raise RuntimeError("rate limited")

    res = await aml.screen_full("Twhatever", Boom())
    assert res.clear              # a failing live check must not wedge orders


async def test_screen_full_respects_disable_flag(monkeypatch):
    monkeypatch.setattr(aml, "_blacklist", {})
    monkeypatch.setattr(aml.settings, "aml_tether_check", False)
    tron = FakeTron(frozen={"Tfrozen"})
    res = await aml.screen_full("Tfrozen", tron)
    assert res.clear and tron.calls == 0   # check disabled → skipped entirely

"""Env-driven settings. Prefix `SWAP_`; load from a gitignored `.env`.

All secrets (adapter internal key, TRON mnemonic, TronGrid key) live here and
must come from the environment — never hard-code or commit them.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SWAP_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # storage
    db_path: str = "./swap.db"

    # emercoin adapter (EMC delivery via POST /wallet/send)
    adapter_url: str = "http://localhost:8001"
    adapter_internal_key: str = ""

    # TRON watcher source
    trongrid_url: str = "https://api.trongrid.io"
    trongrid_api_key: str = ""
    usdt_contract: str = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"  # mainnet USDT TRC20

    # Single shared deposit address shown to all buyers; payments are matched by
    # a unique per-order amount tag (not by address). Keep its key (e.g. mnemonic
    # index 0) so collected USDT can later be moved to treasury / off-ramp.
    deposit_address: str = ""
    tron_mnemonic: str = ""          # controls deposit_address / cold ops
    sweep_address: str = ""          # treasury target (manual, low-volume)

    # Payment-amount tagging: smallest unit is 1e-6 USDT (TRON USDT = 6 decimals).
    tag_step_units: int = 1          # micro-USDT increment per uniqueness bump
    tag_max_tries: int = 100000      # how many tags to probe before giving up

    # economics
    emc_per_usdt: float = 10.0
    min_usdt: float = 5.0     # below this the TRON gas eats the trade; see docs
    max_usdt: float = 10.0

    # settlement
    confirmations_required: int = 19
    order_ttl_minutes: int = 30

    # EMC reserve: refuse new orders we can't deliver (out-of-service when low).
    emc_reserve_check: bool = True
    emc_reserve_buffer: float = 0.0   # extra EMC kept above outstanding obligations
    delivery_max_retries: int = 5     # deliver_failed retries before manual handling

    # callbacks
    callback_max_retries: int = 6

    # AML
    # OFAC sanctioned TRON addresses (per-chain list auto-built from the OFAC SDN).
    aml_ofac_url: str = (
        "https://raw.githubusercontent.com/0xB10C/"
        "ofac-sanctioned-digital-currency-addresses/lists/sanctioned_addresses_TRX.txt"
    )
    aml_tether_check: bool = True        # live USDT.isBlackListed() per deposit
    aml_refresh_hours: int = 6           # how often to reload the OFAC list


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

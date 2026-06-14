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

    # HD deposit wallet (index = order_id) + sweep target
    tron_mnemonic: str = ""
    sweep_address: str = ""

    # economics
    emc_per_usdt: float = 10.0
    min_usdt: float = 1.0
    max_usdt: float = 10.0

    # settlement
    confirmations_required: int = 19
    order_ttl_minutes: int = 30

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

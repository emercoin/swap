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
    usdt_decimals: int = 6           # TRON USDT = 6 decimals (raw unit ÷ 10^6 = USDT)

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

    # Back-pressure: cap orders awaiting payment so order-creation spam can't
    # exhaust the amount-tag space / DB / expiry sweep (0 disables). Paid orders
    # aren't awaiting, so this never blocks a buyer who actually pays.
    max_awaiting_orders: int = 500

    # EMC reserve: refuse new orders we can't deliver (out-of-service when low).
    emc_reserve_check: bool = True
    emc_reserve_buffer: float = 0.0   # extra EMC kept above outstanding obligations
    delivery_max_retries: int = 5     # deliver_failed retries before manual handling

    # Reserve monitor: proactive balance log + low-reserve alert. The pre-flight
    # above is reactive (a buyer hits a 503 once the wallet is short); this logs the
    # hot-wallet EMC balance and outstanding obligations on a cadence and WARNs while
    # the available headroom is below the watermark, so the operator tops up before
    # going out of service. Default watermark ≈ 3 max-size orders of runway.
    reserve_monitor_enabled: bool = True
    reserve_monitor_interval_minutes: int = 15
    reserve_low_watermark: float = 300.0   # EMC; warn while available drops below this

    # callbacks
    callback_max_retries: int = 6

    # public web channel (raw on-ramp for humans): a first-party "web" service
    # the browser drives without holding any key. The service-to-service API
    # (X-API-Key + callback) stays the primary interface; this only adds a public
    # surface. Orders created here carry no callback_url (the page polls instead).
    web_channel_enabled: bool = True
    web_service_name: str = "web"          # first-party service row driving /web
    web_rate_per_min: int = 6              # max order creations per client IP / min
    web_max_concurrent_per_ip: int = 5     # max orders one IP can hold open at once
    #                                        (approximated over the order-TTL window)
    # Proof-of-work on public order creation — defense-in-depth vs distributed
    # floods that beat IP limits (a botnet). The browser must solve a hashcash
    # challenge from GET /web/challenge before POST /web/order is accepted, so each
    # order costs ~2^bits hashes. Tune bits down if mobile clients are too slow.
    web_pow_enabled: bool = True
    web_pow_bits: int = 20                 # required leading zero bits (~2^bits hashes)
    web_pow_ttl_seconds: int = 300         # how long an issued challenge stays valid
    serve_static: bool = False             # serve swap/site/ from the app (local dev;
    #                                        in prod Caddy serves the static corpus)

    # Public stats digest (/stats.html + GET /web/stats): proof-of-reserves style
    # transparency page. The endpoint is keyless and public, so a short TTL cache
    # shields the adapter + TronGrid from being hammered on every page hit.
    stats_cache_ttl_seconds: int = 60

    # MCP exchanger surface: the keyless web on-ramp re-exposed as agent tools
    # (buy_emc / order_status / cancel_order / swap_config), mounted at /mcp over
    # Streamable HTTP. Shares the web channel's anti-spam (global cap + per-IP).
    mcp_enabled: bool = True
    # DNS-rebinding protection is for browser-driven localhost servers; here the
    # edge (Caddy/CF) validates Host and callers are server-side agents, so off.
    mcp_dns_rebinding_protection: bool = False

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

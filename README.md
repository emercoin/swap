# swap — EMC cashier (USDT → EMC)

A **dumb till** with one primitive. All business logic stays in the calling
services; swap knows nothing about NVS/DNS/subscriptions.

```
buy_emc(amount_usdt, destination_emc_address, callback_url, ref)
  → collect USDT on a unique deposit address
  → on confirmation, deliver EMC (fixed rate ×10) to destination
  → notify the caller with a SIGNED callback
```

`destination` is opaque to swap: it can be a **service's** address (the service
then renders its own product on that EMC — the user only ever pays USDT and
never touches a wallet) or the **user's own** address (raw on-ramp).

See [`FOR_CLAUDE_TODO.md`](./FOR_CLAUDE_TODO.md) for the full design rationale.

## Locked decisions

| Topic | Decision |
|-------|----------|
| Rate | static **1 USDT = 10 EMC** |
| Cap / floor | **5–10 USDT** (floor 5: below it TRON gas dominates) |
| USDT rail | **TRC20 (TRON)** |
| Payment match | **one shared deposit address + unique per-order amount tag** |
| EMC delivery | via **emercoin adapter** `POST /wallet/send` (`X-Internal-Key`) |
| Callback signature | **HMAC-SHA256** over canonical body, per-service secret |
| KYC | none (amounts far below threshold) |
| AML | minimal but mandatory — OFAC SDN + Tether freeze blacklist |
| Terms | exact amount, single transfer, **one-way (no refunds)** — state in the offer |

## Layout

```
swap/
  config.py        env-driven settings (pydantic-settings)
  models.py        OrderStatus enum + request/response schemas
  states.py        order state machine (allowed transitions)
  schema.sql       DDL: services/orders/deposits/aml_checks/sweeps/callbacks
  db.py            SQLite connection + init
  repository.py    DB access layer
  auth.py          caller auth by API key
  orders.py        buy_emc business logic (shared by REST + MCP)
  main.py          FastAPI app (REST: POST /buy_emc, GET /order/{id})
  mcp_app.py       FastMCP wrapper (buy_emc + status as MCP tools)
  web.py           public keyless /web/* channel (raw on-ramp for humans)
  site/            static exchanger page + offer (index.html, oferta.html)
  clients/
    adapter.py     EMC delivery + balance via emercoin adapter
    trongrid.py    TRC20 deposit watcher source (TronGrid)
  tron/
    hd.py          HD derivation of deposit addresses (BIP44, coin 195)
  services/
    aml.py         OFAC + Tether blacklist screening
    delivery.py    deliver EMC from reserve (idempotent)
    callback.py    signed callback notifier + retries
    watcher.py     background loop: deposits → confirm → AML → deliver → notify
    sweep.py       USDT consolidation from deposit addresses
```

## State machine

```
created → awaiting_payment → confirmed → emc_delivered → notified (done)
                                ↘ underpaid    (top-up or partial refund)
                                ↘ overpaid     (refund excess)
                                ↘ aml_hold     (sender blacklisted → manual)
                                ↘ deliver_failed (retry; else refund USDT)
expired — no payment before TTL
```

## Dev

```bash
uv sync --extra dev
cp .env.example .env          # fill secrets
uv run uvicorn swap.main:app --reload --port 8002
```

EMC delivery and the TRON watcher need the emercoin adapter and TronGrid creds;
for local end-to-end you can bring up the node+adapter from `emercoin_docker`
(`docker compose --profile dev up`). TRON parts are verified in the sandbox
before they are wired into the watcher.

> **Schema changes have no migrations.** `db.py` applies `schema.sql` with
> `CREATE TABLE IF NOT EXISTS`, which does **not** alter an existing table. After
> editing `schema.sql` in dev, reset the database: `rm swap.db` (then restart —
> it recreates the schema — and re-run `scripts/register_service` since the
> `services` table is wiped too). `swap.db` holds only local/test data.

> Status: **full happy path verified live end-to-end** (+ 41 unit tests). On
> 2026-06-14 a real run took a TRC20 USDT deposit on TRON **Nile testnet** →
> `confirmed` → delivered real **EMC on Emercoin mainnet** (via the adapter
> `/wallet/send`) → **signed callback** verified by the receiver against the
> service's HMAC secret:
> `awaiting_payment → confirmed → emc_delivered → notified`.
> See `docs/TESTNET.md` for the runbook (`scripts/testnet/`).
>
> AML is live: OFAC SDN addresses (TRON) loaded into memory + refreshed, and a
> per-deposit live Tether `isBlackListed` check; a hit → `aml_hold` (no delivery).
>
> Payment matching: pivoted from a unique HD address per order to **one shared
> deposit address + a unique per-order amount tag** (matched by exact amount).
> This removes per-order sweeping and the fresh-address gas penalty; the trade-off
> is exact-amount, single-transfer payments (no auto under/overpaid). Collected
> USDT is moved to treasury / off-ramp manually at low volume.
> Deferred: USDT sweep / TRON tx signing (`services/sweep.py`, kept off the hot
> path) and MCP transport-level auth.

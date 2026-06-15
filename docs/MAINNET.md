# Mainnet cutover runbook (`swap.emercoin.com`)

Switches the deployed staging stack from TRON **testnet (Nile)** to **mainnet**,
where USDT is real. EMC delivery is already real on both (the node wallet), so the
only change is the TRON side: network, USDT contract, and a fresh deposit address.

> Two safety rules that drive every step below:
> * The watcher only **reads** TRON — the spend key (mnemonic) never goes on the
>   host. Only the public deposit address does.
> * The deposit address must be **fresh** (no prior USDT history) so testnet/old
>   orders can't replay onto a clean DB, and so the amount-tag matching starts empty.

## 1. Generate the mainnet deposit wallet (offline)

On an offline machine:

```bash
uv run python -m scripts.gen_deposit_wallet
```

Prints a new 24-word BIP39 mnemonic and the matching TRON address. Store the
mnemonic offline (paper / password manager) — it is the spend key for all USDT
collected on that address. Copy only the printed `SWAP_DEPOSIT_ADDRESS=...` line.

## 2. Edit the droplet `.env`

In `/opt/swap/deploy/.env` flip the four TRON lines from Nile to mainnet:

```ini
SWAP_TRONGRID_URL=https://api.trongrid.io
SWAP_TRONGRID_API_KEY=<mainnet TronGrid api key>
SWAP_USDT_CONTRACT=TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t   # mainnet USDT TRC20
SWAP_DEPOSIT_ADDRESS=<address from step 1>
```

Leave `SWAP_TRON_MNEMONIC` empty. Keep `SWAP_ADAPTER_INTERNAL_KEY` (the shared EMC
payout rail) and the economics/safety defaults as they are.

## 3. Wipe the staging (testnet) database

Testnet orders and used amount tags must not carry over. With the stack down:

```bash
docker compose -f deploy/docker-compose.droplet.yaml down
docker volume rm swap_swap_data        # or: rm the mounted swap.db
```

After the next start, re-run `scripts/register_service` for any service whose API
key lived only in the wiped DB (the first-party `web` service is auto-created).

## 4. Bring it up and re-check

```bash
cd /opt/swap
docker compose -f deploy/docker-compose.droplet.yaml --env-file deploy/.env up -d --build
```

Run the readiness check against the live config (read-only, changes nothing):

```bash
docker compose -f deploy/docker-compose.droplet.yaml exec swap python -m scripts.preflight
```

Expect all green except informational warnings. It must confirm: mainnet URL +
mainnet USDT contract, a valid and **unused** deposit address, TronGrid reachable,
`isBlackListed()` callable, the EMC reserve covers at least one max order, and a
clean DB. Resolve any `FAIL` before announcing the endpoint.

## 5. Reserve and smoke test

- Ensure the node wallet holds enough EMC for expected volume (the reserve
  pre-flight returns `503` and takes no USDT when it can't cover an order).
- Optional real on-ramp: pay the minimum (5 USDT → 50 EMC) to an address you
  control via `https://swap.emercoin.com/web/order`, confirm it reaches `notified`.

## Rollback

Restore the four Nile lines in `.env`, wipe the DB again, and `up -d` to return to
testnet staging.

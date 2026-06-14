# Testnet (Nile) end-to-end runbook

Validates the TRON watcher with **zero real funds**: a throwaway payer wallet
sends our own test TRC20 to a swap deposit address; we watch the order reach
`confirmed`. The payer side is scripted (`scripts/testnet/`) — no browser/GUI.

> Two wallets, don't confuse them:
> * **swap deposit wallet** — HD, derived in code from `SWAP_TRON_MNEMONIC`. No GUI.
> * **payer wallet** — the test "user/agent" that sends USDT. This is what the
>   scripts manage (`payer.key`, gitignored).

Install the tooling: `uv sync --extra dev --extra testnet`

## 1. Payer wallet + faucet (one manual step)

```bash
uv run python -m scripts.testnet.wallet
```
Prints the payer address. **Fund it with test TRX** from the faucet it prints
(<https://nileex.io/join/getJoinPage>) — needed for gas. Re-run to see the TRX
balance land.

## 2. Deploy the test TRC20

```bash
uv run python -m scripts.testnet.deploy_token
```
Compiles `TestUSDT.sol`, deploys to Nile, mints 1,000,000 test-USDT to the payer,
and prints the contract address (also saved to `state.json`).

## 3. Point swap at Nile + this token

In `.env`:
```
SWAP_TRONGRID_URL=https://nile.trongrid.io
SWAP_USDT_CONTRACT=<address from step 2>
SWAP_TRON_MNEMONIC=<your testnet mnemonic>     # generates deposit addresses
```
Run swap and register a calling service:
```bash
uv run uvicorn swap.main:app --port 8002        # in one shell
uv run python -m scripts.register_service "e2e-test"   # copy its api_key
```

## 4. Run the end-to-end

```bash
uv run python -m scripts.testnet.e2e --api-key <swk_...> --amount 5
```
It creates an order, sends 5 test-USDT to the returned deposit address, and polls
until the order reaches **`confirmed`** (deposit detected, AML-screened, amount
matched). To send a payment manually instead:
```bash
uv run python -m scripts.testnet.pay <deposit_address> 5
```

### Full path to `notified` (signed callback)

To exercise the signed callback end-to-end, run the receiver (it verifies the
HMAC with the service's `callback_secret` from `register_service`) and point the
order's callback at it:
```bash
# shell C — callback receiver
uv run python -m scripts.testnet.callback_server --secret <callback_secret> --port 9009

# then run e2e with --callback-url; it now polls through to `notified`
uv run python -m scripts.testnet.e2e --api-key <swk_...> --amount 1 \
    --dest <emc_address> --callback-url http://localhost:9009/cb
```
The receiver prints `VERIFIED callback: ...` on a good signature (→ order
`notified`) and `REJECTED ... (bad signature)` + 401 on a forged one — that 401
is the protection against a forged 'paid'.

## Scope

This proves the watcher path: `awaiting_payment → confirmed`. Reaching
`emc_delivered` / `notified` additionally needs the emercoin **adapter** running
with an EMC reserve (it signs `/wallet/send`) and a reachable `callback_url` — a
separate setup from the TRON watcher test.

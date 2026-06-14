-- swap schema (SQLite on start). Timestamps are ISO-8601 UTC text.
-- Idempotency: one order per (service_id, ref); one delivery per order.

PRAGMA foreign_keys = ON;

-- Calling services: authenticated by API key, callbacks signed with their secret.
CREATE TABLE IF NOT EXISTS services (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    api_key         TEXT NOT NULL UNIQUE,
    callback_secret TEXT NOT NULL,           -- HMAC-SHA256 key for this service
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Orders: one buy_emc request. HD deposit-address index = id.
CREATE TABLE IF NOT EXISTS orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id          INTEGER NOT NULL REFERENCES services(id),
    ref                 TEXT NOT NULL,                 -- caller's invoice id
    amount_usdt         REAL NOT NULL,
    emc_amount          REAL NOT NULL,                 -- amount_usdt * rate snapshot
    destination_emc     TEXT NOT NULL,
    callback_url        TEXT NOT NULL,
    deposit_address     TEXT UNIQUE,                   -- TRON HD addr (set after id known)
    status              TEXT NOT NULL DEFAULT 'created',
    emc_txid            TEXT,                          -- set once delivered
    expires_at          TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (service_id, ref)                           -- idempotency
);

CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_deposit ON orders(deposit_address);

-- Deposits: incoming TRC20 transfers seen on a deposit address.
CREATE TABLE IF NOT EXISTS deposits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL REFERENCES orders(id),
    tron_txid       TEXT NOT NULL UNIQUE,              -- dedupe re-seen transfers
    from_address    TEXT NOT NULL,
    amount_usdt     REAL NOT NULL,
    confirmations   INTEGER NOT NULL DEFAULT 0,
    seen_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- AML screening of the sender address.
CREATE TABLE IF NOT EXISTS aml_checks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    address     TEXT NOT NULL,
    result      TEXT NOT NULL,                         -- clear | hit
    source      TEXT,                                  -- ofac | tether | ...
    checked_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Sweeps: consolidation of USDT off deposit addresses (needs TRX gas).
CREATE TABLE IF NOT EXISTS sweeps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    tron_txid   TEXT,
    amount_usdt REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',       -- pending | sent | confirmed | failed
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Callbacks: signed notifications + retry bookkeeping.
CREATE TABLE IF NOT EXISTS callbacks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL REFERENCES orders(id),
    url             TEXT NOT NULL,
    payload         TEXT NOT NULL,                     -- exact signed JSON body
    attempts        INTEGER NOT NULL DEFAULT 0,
    delivered       INTEGER NOT NULL DEFAULT 0,
    last_status     INTEGER,                           -- last HTTP status
    next_retry_at   TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

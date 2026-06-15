"""One-off migration: amount-tag reuse (drop the global UNIQUE on orders.amount_usdt).

Background: tags used to be globally unique forever, so the pay amount crept up
(5.000001, 5.000002, …) as orders accumulated. The new schema scopes uniqueness to
ACTIVE orders via a partial index (idx_orders_active_amount), letting terminal orders
release their tag for reuse. New databases get this from schema.sql; existing ones
carry the old column-level `UNIQUE(amount_usdt)` baked into the table, which SQLite
can only remove by rebuilding the table — that's what this does, preserving every
row and the foreign keys that reference orders(id).

Idempotent: it no-ops if the global UNIQUE is already gone. Safe to run on a live DB
(single transaction). Runnable in the container (scripts/ is copied into the image):

    python -m scripts.migrate_tag_reuse           # uses SWAP_DB_PATH from the env
    python -m scripts.migrate_tag_reuse /path/to/swap.db
"""
from __future__ import annotations

import sqlite3
import sys

from swap.config import settings

ACTIVE = "'awaiting_payment', 'confirmed', 'deliver_failed', 'aml_hold', 'emc_delivered'"

# Orders table without the column-level UNIQUE on amount_usdt (keeps UNIQUE(service_id, ref)).
NEW_TABLE = """
CREATE TABLE orders_new (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id          INTEGER NOT NULL REFERENCES services(id),
    ref                 TEXT NOT NULL,
    amount_usdt         REAL NOT NULL,
    emc_amount          REAL NOT NULL,
    destination_emc     TEXT NOT NULL,
    callback_url        TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'awaiting_payment',
    emc_txid            TEXT,
    delivery_attempts   INTEGER NOT NULL DEFAULT 0,
    expires_at          TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (service_id, ref)
)
"""

_COLS = ("id, service_id, ref, amount_usdt, emc_amount, destination_emc, callback_url, "
         "status, emc_txid, delivery_attempts, expires_at, created_at, updated_at")


def _has_global_unique(con: sqlite3.Connection) -> bool:
    """True while a UNIQUE constraint covers amount_usdt alone (the old schema)."""
    for _, name, unique, origin, _ in con.execute("PRAGMA index_list('orders')"):
        if unique and origin == "u":                       # from a UNIQUE table constraint
            cols = [r[2] for r in con.execute(f"PRAGMA index_info('{name}')")]
            if cols == ["amount_usdt"]:
                return True
    return False


def migrate(db_path: str) -> bool:
    con = sqlite3.connect(db_path)
    try:
        if not _has_global_unique(con):
            print(f"{db_path}: already migrated (no global UNIQUE on amount_usdt) — nothing to do")
            return False
        con.execute("PRAGMA foreign_keys=OFF")
        con.executescript(
            "BEGIN;"
            + NEW_TABLE + ";"
            + f"INSERT INTO orders_new ({_COLS}) SELECT {_COLS} FROM orders;"
            + "DROP TABLE orders;"
            + "ALTER TABLE orders_new RENAME TO orders;"
            + "CREATE INDEX idx_orders_status ON orders(status);"
            + "CREATE INDEX idx_orders_amount ON orders(amount_usdt);"
            + f"CREATE UNIQUE INDEX idx_orders_active_amount ON orders(amount_usdt) WHERE status IN ({ACTIVE});"
            + "COMMIT;"
        )
        broken = con.execute("PRAGMA foreign_key_check").fetchall()
        con.execute("PRAGMA foreign_keys=ON")
        if broken:
            raise SystemExit(f"foreign_key_check failed after migration: {broken}")
        n = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        print(f"{db_path}: migrated — orders table rebuilt without the global UNIQUE ({n} rows preserved)")
        return True
    finally:
        con.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else settings.db_path)

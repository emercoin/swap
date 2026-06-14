"""SQLite connection + schema init.

A single shared aiosqlite connection (SQLite serialises writes anyway). The
repository layer is the only place that touches SQL, so moving to Postgres/
asyncpg later means reimplementing `repository.py` and this module — nothing
above them changes.
"""
from __future__ import annotations

from pathlib import Path

import aiosqlite

from .config import settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_conn: aiosqlite.Connection | None = None


async def connect() -> aiosqlite.Connection:
    """Open (once) the shared connection and apply the schema."""
    global _conn
    if _conn is not None:
        return _conn
    conn = await aiosqlite.connect(settings.db_path)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.executescript(SCHEMA_PATH.read_text())
    await conn.commit()
    _conn = conn
    return conn


async def get_conn() -> aiosqlite.Connection:
    if _conn is None:
        return await connect()
    return _conn


async def close() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None

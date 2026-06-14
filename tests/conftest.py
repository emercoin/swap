"""Test fixtures: an isolated SQLite db per test, no network, no watcher."""
from __future__ import annotations

import pytest_asyncio

from swap import db


@pytest_asyncio.fixture
async def fresh_db(tmp_path, monkeypatch):
    """Point the db module at a throwaway file and apply the schema."""
    monkeypatch.setattr(db.settings, "db_path", str(tmp_path / "test.db"))
    await db.close()
    conn = await db.connect()
    yield conn
    await db.close()

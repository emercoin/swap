"""Test fixtures: an isolated SQLite db per test, no network, no watcher."""
from __future__ import annotations

import pytest
import pytest_asyncio

from swap import db


@pytest.fixture(autouse=True)
def _pin_economics(monkeypatch):
    """Pin economics to known defaults so the suite never depends on a local .env."""
    monkeypatch.setattr(db.settings, "emc_per_usdt", 10.0)
    monkeypatch.setattr(db.settings, "min_usdt", 5.0)
    monkeypatch.setattr(db.settings, "max_usdt", 10.0)


@pytest_asyncio.fixture
async def fresh_db(tmp_path, monkeypatch):
    """Point the db module at a throwaway file and apply the schema."""
    monkeypatch.setattr(db.settings, "db_path", str(tmp_path / "test.db"))
    await db.close()
    conn = await db.connect()
    yield conn
    await db.close()

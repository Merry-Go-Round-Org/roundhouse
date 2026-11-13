"""Shared fixtures. Tests use a temporary SQLite file and make no network or Docker calls."""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from harness.db import create_tables, enable_sqlite_pragmas


class Database:
    """Created inside each test's event loop, because async engines are bound to one loop."""

    def __init__(self, path) -> None:
        self.url = f"sqlite+aiosqlite:///{path}"
        self.engine = None

    async def __aenter__(self):
        self.engine = create_async_engine(self.url)
        enable_sqlite_pragmas(self.engine)
        async with self.engine.begin() as connection:
            await connection.run_sync(create_tables)
        return async_sessionmaker(self.engine, expire_on_commit=False)

    async def __aexit__(self, *_exc) -> None:
        await self.engine.dispose()


@pytest.fixture
def database(tmp_path) -> Database:
    return Database(tmp_path / "harness.db")

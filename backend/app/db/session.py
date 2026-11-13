"""Async SQLite connections. Each request gets its own session."""

from collections.abc import AsyncIterator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app.config import settings
from app.db.tables import Base
from sqlalchemy import event
from app.services.harness_adapter import prepare_engine, create_harness_tables

engine = create_async_engine(settings.resolved_database_url)
prepare_engine(engine)


@event.listens_for(engine.sync_engine, "connect")
def enable_sqlit_foreign_keys(dbapi_connection, connection_record):
    if engine.dialect.name == "sqlite":
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys = ON")
        finally:
            cursor.close()


SessionFactory = async_sessionmaker(engine, expire_on_commit=False)


async def initialize_database() -> None:
    settings.storage_path.mkdir(parents=True, exist_ok=True)
    for folder in ("imports", "datasets", "artifacts", "logs"):
        (settings.storage_path / folder).mkdir(exist_ok=True)
    import app.services.ai_native_service  # noqa: F401  registers the AI-native tables

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(create_harness_tables)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session

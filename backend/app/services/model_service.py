"""Model metadata persistence; archived models leave the active inventory."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.tables import ModelRecord
from app.schemas.models import ModelCreate

async def create_model(session: AsyncSession, payload: ModelCreate) -> ModelRecord:
    record = ModelRecord(**payload.model_dump())
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record

async def list_models(session: AsyncSession, offset: int, limit: int) -> list[ModelRecord]:
    result = await session.scalars(select(ModelRecord).where(
        ModelRecord.lifecycle_status != "deleted"
    ).order_by(
        ModelRecord.created_at, ModelRecord.id
    ).offset(offset).limit(limit))
    return list(result)

async def get_model(session: AsyncSession, model_id: str) -> ModelRecord | None:
    model = await session.get(ModelRecord, model_id)
    return model if model and model.lifecycle_status != "deleted" else None

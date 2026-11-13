"""Selected dataset metadata, not proof of a local download or evaluated model."""
from fastapi import APIRouter
from app.schemas.models import DatasetRead
from app.services.dataset_catalog import DATASET_CATALOG

router = APIRouter(prefix="/datasets", tags=["Datasets"])

@router.get("", response_model=list[DatasetRead])
async def list_datasets():
    return DATASET_CATALOG

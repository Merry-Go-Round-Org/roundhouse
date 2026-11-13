"""The datasets the platform knows about, and what has been uploaded for each.

The catalog is selected metadata, not proof of a local download or an evaluated model.
Uploaded versions come from the immutable dataset store.
"""
from sqlalchemy import select

from app.schemas.models import DatasetRead

DATASET_CATALOG = [
    DatasetRead(
        id="uci_credit",
        name="UCI Default of Credit Card Clients",
        filename="default of credit card clients.xls",
        target_description="1 = default; 0 = no default",
        limitations=[
            "Customer snapshot; no genuine temporal cohorts",
            "Historical Taiwan population",
        ],
    ),
    DatasetRead(
        id="ulb_fraud",
        name="ULB Credit Card Fraud",
        filename="creditcard.csv",
        target_description="Class: 1 = fraud; 0 = genuine",
        limitations=[
            "Protected-group fairness not assessable from published attributes",
            "Two-day observation window",
            "Original PCA feature meanings unavailable",
        ],
    ),
]


def _version(record):
    # Descriptor facts and partition sizes only. Per-partition outcome rates and hashes
    # stay out, so no agent learns anything about the test partition beyond its size.
    d = record.descriptor
    return {
        "version": record.version,
        "uploaded_at": record.created_at.isoformat() if record.created_at else None,
        "source_url": d.get("source_url"),
        "license": d.get("license"),
        "sha256": record.sha256,
        "target_column": d.get("target_column"),
        "positive_class": d.get("positive_class"),
        "audit_only_columns": d.get("audit_only_columns", []),
        "time_column": d.get("time_column"),
        "row_count": d.get("row_count"),
        "partition_counts": d.get("partition_counts"),
        "columns": record.columns,
    }


async def describe_datasets(session):
    """Catalog entries with every uploaded version of each, newest first."""
    from app.services.upload_service import DatasetRecord
    from app.services.dataset_deletion import visible_datasets

    records = list(
        await session.scalars(
            visible_datasets().order_by(DatasetRecord.created_at.desc())
        )
    )
    return [
        {
            **entry.model_dump(),
            "uploaded_versions": [
                _version(r) for r in records if r.dataset_id == entry.id
            ],
        }
        for entry in DATASET_CATALOG
    ]

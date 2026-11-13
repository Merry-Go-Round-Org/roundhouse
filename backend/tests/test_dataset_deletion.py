"""Dataset soft deletion hides versions no live model uses and keeps their files."""
import asyncio
from uuid import uuid4
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app.main import app
from app.config import settings
from app.db.session import SessionFactory
from app.db.tables import AuditRecord, ModelRecord
from app.services.identity import Actor
from app.services import dataset_deletion
from app.services.upload_service import DatasetRecord, InventoryRecord, UploadRecord, dataset_root


def seed(count=2):
    async def create():
        ids = []
        async with SessionFactory() as session:
            for n in range(count):
                version_id = str(uuid4())
                record = DatasetRecord(id=version_id, dataset_id="delete_fixture_" + uuid4().hex[:8], version="delete-" + uuid4().hex[:8],
                                       sha256="a" * 64, split_sha256="b" * 64,
                                       descriptor={"name": "Test data", "source_filename": "source.csv", "target_column": "target", "positive_class": 1}, columns=[], statistics={})
                session.add(record)
                session.add(AuditRecord(actor_id="developer-one", resource_id=version_id, action="dataset_uploaded", payload={}))
                ids.append(version_id)
            await session.commit()
        return ids
    return asyncio.run(create())


def test_unreferenced_versions_hidden_and_files_kept():
    with TestClient(app):
        ids = seed()
        for version_id in ids:
            root = settings.storage_path / "datasets" / version_id
            root.mkdir(parents=True)
            (root / "source").write_text("demo")
        async def exercise():
            async with SessionFactory() as session:
                who = Actor("developer-one", "developer")
                view = await dataset_deletion.preview(session, ids, who)
                assert view["can_delete"]
                request = dataset_deletion.DeleteRequest(dataset_version_ids=ids, preview_token=view["preview_token"], request_id=uuid4())
                result = await dataset_deletion.remove(session, request, who)
                assert result["deleted"] == ids and result["failed"] == []
                assert await dataset_deletion.remove(session, request, who) == result
                visible = {row.id for row in await session.scalars(dataset_deletion.visible_datasets())}
                for version_id in ids:
                    assert version_id not in visible
                    assert await session.get(DatasetRecord, version_id) is not None
                    assert (settings.storage_path / "datasets" / version_id / "source").exists()
                record = await session.get(DatasetRecord, ids[0])
                await dataset_deletion.restore(session, record, who.id, "test")
                await session.commit()
                assert not await dataset_deletion.is_deleted(session, ids[0])
        asyncio.run(exercise())


def test_live_model_blocks_until_model_deleted():
    with TestClient(app):
        ids = seed()
        async def exercise():
            async with SessionFactory() as session:
                selected_dataset = await session.get(DatasetRecord, ids[1])
                model = ModelRecord(name="dataset_ref_" + uuid4().hex[:8], version="1", language="R", dataset_id=selected_dataset.dataset_id, purpose="Test")
                session.add(model)
                await session.flush()
                upload = UploadRecord(id=str(uuid4()), status="completed", actor_id="developer-one", archive_sha256="a" * 64,
                                      checks=[], files=[], model_id=model.id)
                session.add(upload)
                await session.flush()
                session.add(InventoryRecord(model_id=model.id, name=model.name, model_version=model.version,
                                            upload_id=upload.id, dataset_version_id=ids[1], manifest={}, provenance={}, bundle_sha256="b" * 64))
                await session.commit()
                who = Actor("developer-one", "developer")
                view = await dataset_deletion.preview(session, ids, who)
                assert not view["can_delete"] and view["datasets"][1]["blockers"] == {"registered_models": [model.id]}
                assert view["datasets"][0]["blockers"] == {}
                try:
                    await dataset_deletion.remove(session, dataset_deletion.DeleteRequest(
                        dataset_version_ids=ids, preview_token=view["preview_token"], request_id=uuid4()), who)
                except HTTPException as exc:
                    assert exc.status_code == 409
                else:
                    assert False
                assert not any([await dataset_deletion.is_deleted(session, i) for i in ids])
                other = await dataset_deletion.preview(session, [ids[0]], Actor("developer-two", "developer"))
                assert "authorization" in other["datasets"][0]["blockers"]
                model.lifecycle_status = "deleted"
                await session.commit()
                assert (await dataset_deletion.preview(session, ids, who))["can_delete"]
        asyncio.run(exercise())

"""Bulk soft deletion hides models, keeps evidence and blocks only active work."""
import asyncio
from uuid import uuid4
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from app.main import app
from app.config import settings
from app.db.session import SessionFactory
from app.db.tables import ModelRecord, PackageRecord, runningRecord
from app.services.identity import Actor
from app.services import model_deletion, model_service
from app.services.upload_service import UploadRecord, InventoryRecord


def seed(count=3):
    async def create():
        ids = []
        async with SessionFactory() as session:
            for _ in range(count):
                model = ModelRecord(name="remove_fixture_" + uuid4().hex[:8], version="1.0", language="R", dataset_id="uci_credit", purpose="Test")
                session.add(model)
                await session.flush()
                upload = UploadRecord(id=str(uuid4()), actor_id="developer-one", status="completed", archive_sha256="a" * 64, checks=[], files=[], model_id=model.id)
                session.add(upload)
                await session.flush()
                session.add(InventoryRecord(model_id=model.id, name=model.name, model_version=model.version, upload_id=upload.id, dataset_version_id=None, manifest={}, provenance={}, bundle_sha256="b" * 64))
                package_sha = uuid4().hex + uuid4().hex
                session.add(PackageRecord(id=str(uuid4()), model_id=model.id, name=model.name,
                                          version=model.version, dataset_id=model.dataset_id,
                                          language=model.language, sha256=package_sha,
                                          input_sha256="c" * 64, file_hashes={}, n_rows=0))
                ids.append((model.id, upload.id, package_sha))
            await session.commit()
        return ids
    return asyncio.run(create())


def test_soft_delete_hides_models_and_keeps_evidence():
    with TestClient(app):
        ids = seed()
        upload_root = settings.storage_path / "uploads" / ids[0][1]
        upload_root.mkdir(parents=True)
        (upload_root / "bundle.zip").write_bytes(b"test")
        async def exercise():
            async with SessionFactory() as session:
                picked = [ids[0][0], ids[1][0]]
                # Finished evidence (the automatic document run included) no longer blocks.
                session.add(runningRecord(model_id=picked[0], model_version="1.0", dataset_id="uci_credit",
                                          operation="document", status="completed"))
                await session.commit()
                who = Actor("developer-one", "developer")
                view = await model_deletion.preview(session, picked, who)
                assert view["can_delete"] and len(view["models"]) == 2
                assert view["models"][0]["related_counts"]["runs"] == 1
                request = model_deletion.DeleteRequest(model_ids=picked, preview_token=view["preview_token"], request_id=uuid4())
                result = await model_deletion.remove(session, request, who)
                assert result["deleted"] == picked and result["failed"] == []
                assert await model_deletion.remove(session, request, who) == result
                assert await model_deletion.receipt(session, request.request_id, who) == result
                listed = {m.id for m in await model_service.list_models(session, 0, 100)}
                assert ids[2][0] in listed and not listed & set(picked)
                for model_id, upload_id, _ in ids[:2]:
                    assert await model_service.get_model(session, model_id) is None
                    model = await session.get(ModelRecord, model_id)
                    assert model.lifecycle_status == "deleted"
                    assert await session.get(InventoryRecord, model_id) is not None
                    assert await session.get(UploadRecord, upload_id) is not None
                assert (upload_root / "bundle.zip").exists()
                assert await session.scalar(select(func.count()).select_from(runningRecord).where(runningRecord.model_id == picked[0])) == 1
                # Late work cannot move a deleted model back into the inventory.
                model = await session.get(ModelRecord, picked[0])
                model.lifecycle_status = "approved"
                await session.commit()
                await session.refresh(model)
                assert model.lifecycle_status == "deleted"
                # A deleted model cannot be previewed again.
                try:
                    await model_deletion.preview(session, picked, who)
                except HTTPException as exc:
                    assert exc.status_code == 404
                else:
                    assert False
                # An explicit restore brings back the earlier stage.
                await model_deletion.restore(session, model, who.id, "test")
                await session.commit()
                assert (await session.get(ModelRecord, picked[0])).lifecycle_status == "development"
                assert await session.get(model_deletion.DeletedModel, picked[0]) is None
        asyncio.run(exercise())


def test_active_run_blocks_whole_batch_and_wrong_role_rejected():
    with TestClient(app):
        ids = seed(2)
        async def exercise():
            async with SessionFactory() as session:
                session.add(runningRecord(model_id=ids[1][0], model_version="1.0", dataset_id="uci_credit", operation="document", status="running"))
                await session.commit()
                selected = [item[0] for item in ids]
                who = Actor("developer-one", "developer")
                view = await model_deletion.preview(session, selected, who)
                assert not view["can_delete"]
                assert view["models"][0]["blockers"] == [] and view["models"][1]["blockers"]
                try:
                    await model_deletion.remove(session, model_deletion.DeleteRequest(model_ids=selected, preview_token=view["preview_token"], request_id=uuid4()), who)
                except HTTPException as exc:
                    assert exc.status_code == 409
                else:
                    assert False
                assert all([(await session.get(ModelRecord, i)).lifecycle_status != "deleted" for i in selected])
                other = await model_deletion.preview(session, [selected[0]], Actor("developer-two", "developer"))
                assert not other["can_delete"] and other["models"][0]["blockers"]
                admin = await model_deletion.preview(session, [selected[0]], Actor("admin-one", "admin"))
                assert admin["can_delete"]
                try:
                    await model_deletion.preview(session, selected, Actor("validator", "validator"))
                except HTTPException as exc:
                    assert exc.status_code == 403
                else:
                    assert False
        asyncio.run(exercise())


def test_stale_preview_rejected():
    with TestClient(app):
        ids = seed(1)
        async def exercise():
            async with SessionFactory() as session:
                selected = [ids[0][0]]
                who = Actor("developer-one", "developer")
                view = await model_deletion.preview(session, selected, who)
                model = await session.get(ModelRecord, selected[0])
                model.version = "2.0"
                await session.commit()
                try:
                    await model_deletion.remove(session, model_deletion.DeleteRequest(model_ids=selected, preview_token=view["preview_token"], request_id=uuid4()), who)
                except HTTPException as exc:
                    assert exc.status_code == 409
                else:
                    assert False
                assert (await session.get(ModelRecord, selected[0])).lifecycle_status != "deleted"
        asyncio.run(exercise())

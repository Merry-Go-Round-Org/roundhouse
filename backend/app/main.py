"""FastAPI backend; cookie-authenticated shared workspace with durable jobs."""

from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.db.session import engine, initialize_database, SessionFactory
from app.api import (
    auth,
    chat,
    dashboard,
    findings,
    models,
    monitoring,
    reports,
    reviews,
    runs,
    packages,
    uploads,
    proposals,
    intake,
)
from app.services import chat_service
from app.services import native_analysis  # Register its additive tables before startup.
from app.services.identity import actor, configured_origins
from app.api.access import router as access_router
from app.api import diagnostics


@asynccontextmanager
async def lifespan(application):
    await initialize_database()
    await chat_service.startup(SessionFactory)
    try:
        yield
    finally:
        await chat_service.shutdown()
        await engine.dispose()


app = FastAPI(
    title=settings.app_name,
    version="0.2.0",
    lifespan=lifespan,
    description="Authenticated model-risk workspace. Upload-derived inventory, evidence, official parity and human reviews.",
)


class RequestGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        if (
            request.method != "OPTIONS"
            and request.url.path.startswith(settings.api_prefix)
            and not request.url.path.startswith(settings.api_prefix + "/auth/")
        ):
            try:
                await actor(request)
            except HTTPException as exc:
                return await JSONResponse(
                    {"detail": exc.detail}, status_code=exc.status_code
                )(scope, receive, send)
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > settings.max_upload_bytes):
            return await JSONResponse(
                {"detail": "Upload exceeds request size limit"}, status_code=413
            )(scope, receive, send)
        count = 0

        async def bounded():
            nonlocal count
            message = await receive()
            count += len(message.get("body", b""))
            if count > settings.max_upload_bytes:
                raise HTTPException(413, detail="Upload exceeds request size limit")
            return message

        await self.app(scope, bounded, send)


app.add_middleware(RequestGuard)
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(configured_origins()),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Content-Type", "Authorization", "Idempotency-Key"],
    expose_headers=["Content-Disposition", "X-Chat-Turn-ID"],
)
app.include_router(access_router)
app.include_router(auth.router, prefix=settings.api_prefix)
for module in (
    intake,
    proposals,
    uploads,
    diagnostics,
    dashboard,
    models,
    runs,
    reports,
    reviews,
    findings,
    monitoring,
    chat,
    packages,
):
    app.include_router(
        module.router, prefix=settings.api_prefix, dependencies=[Depends(actor)]
    )


@app.get(
    "/api/v1/parity-runs/{identity}",
    dependencies=[Depends(actor)],
    tags=["Official parity"],
)
async def get_parity_run(identity: str):
    from app.services.parity_service import ParityRunRecord, parity_read

    async with SessionFactory() as session:
        record = await session.get(ParityRunRecord, identity)
    if record is None:
        raise HTTPException(404, detail="Parity run not found")
    return parity_read(record)


@app.get("/health", tags=["Health"])
async def health():
    return {
        "status": "ok",
        "mode": "authenticated_workspace"
        if not settings.allow_local_demo_auth
        else "local_prototype",
        "harness_connected": True,
        "agent_configured": bool(
            settings.openrouter_api_key
            and settings.openrouter_api_key.get_secret_value()
        ),
    }

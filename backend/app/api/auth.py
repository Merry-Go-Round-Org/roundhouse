"""Password sign-in, revocable cookie sessions and administrator-managed accounts."""

import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import uuid4
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_session
from app.db.auth_tables import UserRecord, SessionRecord, LoginAttempt
from app.db.tables import AuditRecord
from app.services.identity import actor, admin, Actor, trusted_origins
from app.config import settings

router = APIRouter(prefix="/auth", tags=["Authentication"])
Session = Annotated[AsyncSession, Depends(get_session)]
hasher = PasswordHasher()
DUMMY = hasher.hash("not-a-user-password")


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=100)
    password: SecretStr = Field(min_length=1, max_length=256)


class UserCreate(Login):
    password: SecretStr = Field(min_length=12, max_length=256)
    role: Literal["admin", "developer", "validator", "approver"]


def public(user):
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "active": user.active,
    }


@router.post("/login")
async def login(payload: Login, request: Request, response: Response, session: Session):
    origin = request.headers.get("origin")
    if origin and origin not in trusted_origins(request):
        raise HTTPException(403, detail="Untrusted request origin")
    now = datetime.now(timezone.utc)
    key = hashlib.sha256(payload.username.strip().casefold().encode()).hexdigest()
    attempt = await session.get(LoginAttempt, key)
    if (
        attempt
        and attempt.failures >= 10
        and (now - attempt.since.replace(tzinfo=timezone.utc)).total_seconds() < 300
    ):
        raise HTTPException(429, detail="Too many attempts; retry in five minutes")
    user = await session.scalar(
        select(UserRecord).where(
            UserRecord.username == payload.username.strip().casefold()
        )
    )
    try:
        valid = await asyncio.to_thread(
            hasher.verify,
            user.password_hash if user else DUMMY,
            payload.password.get_secret_value(),
        )
    except (VerificationError, InvalidHashError):
        valid = False
    if not user or not user.active or not valid:
        if not attempt:
            attempt = LoginAttempt(key=key, failures=0, since=now)
            session.add(attempt)
        if (now - attempt.since.replace(tzinfo=timezone.utc)).total_seconds() >= 300:
            attempt.since = now
            attempt.failures = 0
        attempt.failures += 1
        await session.commit()
        raise HTTPException(401, detail="Invalid username or password")
    if attempt:
        await session.delete(attempt)
    token = secrets.token_urlsafe(32)
    session.add(
        SessionRecord(
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            user_id=user.id,
            expires_at=now + timedelta(hours=12),
        )
    )
    session.add(
        AuditRecord(actor_id=user.id, resource_id=user.id, action="login", payload={})
    )
    await session.commit()
    response.set_cookie(
        "modelrisk_session",
        token,
        httponly=True,
        secure=settings.auth_cookie_secure
        or request.url.scheme == "https"
        or bool(
            origin
            and origin.startswith("https://")
            and origin in trusted_origins(request)
        ),
        samesite="lax",
        max_age=43200,
        path="/",
    )
    return public(user)


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    session: Session,
    who: Annotated[Actor, Depends(actor)],
):
    token = request.cookies.get("modelrisk_session", "")
    await session.execute(
        delete(SessionRecord).where(
            SessionRecord.token_hash == hashlib.sha256(token.encode()).hexdigest()
        )
    )
    await session.commit()
    response.delete_cookie("modelrisk_session", path="/")
    return {"status": "signed_out"}


@router.get("/me")
async def me(who: Annotated[Actor, Depends(actor)], session: Session):
    # Restore the same public identity after a browser refresh.
    user = await session.get(UserRecord, who.id)
    return public(user) if user else {"id": who.id, "role": who.role}


@router.post("/users", status_code=201)
async def create_user(
    payload: UserCreate, session: Session, who: Annotated[Actor, Depends(admin)]
):
    user = UserRecord(
        id=str(uuid4()),
        username=payload.username.strip().casefold(),
        role=payload.role,
        password_hash=await asyncio.to_thread(
            hasher.hash, payload.password.get_secret_value()
        ),
    )
    session.add(user)
    session.add(
        AuditRecord(
            actor_id=who.id,
            resource_id=user.id,
            action="account_created",
            payload={"role": user.role},
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, detail="Username already exists") from None
    return public(user)


@router.get("/users")
async def users(session: Session, who: Annotated[Actor, Depends(admin)]):
    return [public(u) for u in await session.scalars(select(UserRecord))]


@router.post("/users/{user_id}/disable")
async def disable(
    user_id: str, session: Session, who: Annotated[Actor, Depends(admin)]
):
    if user_id == who.id:
        raise HTTPException(409, detail="Cannot disable your own administrator account")
    user = await session.get(UserRecord, user_id)
    if not user:
        raise HTTPException(404, detail="User not found")
    user.active = False
    await session.execute(delete(SessionRecord).where(SessionRecord.user_id == user_id))
    await session.commit()
    return public(user)

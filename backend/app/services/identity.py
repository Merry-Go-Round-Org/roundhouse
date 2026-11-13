"""Account-backed cookie sessions and role checks; demo identities are opt-in only."""

import hashlib
import secrets
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit
from fastapi import HTTPException, Request
from app.config import settings


@dataclass(frozen=True)
class Actor:
    id: str
    role: str


def configured_origins():
    """Treat the two loopback hostnames as aliases for configured local ports."""
    origins = set(settings.cors_origins)
    for origin in tuple(origins):
        try:
            parsed = urlsplit(origin)
            if (
                parsed.scheme == "http"
                and parsed.hostname in ("localhost", "127.0.0.1")
                and parsed.port is not None
                and not parsed.username
                and not parsed.password
                and parsed.path in ("", "/")
                and not parsed.query
                and not parsed.fragment
            ):
                other = "127.0.0.1" if parsed.hostname == "localhost" else "localhost"
                origins.add(f"http://{other}:{parsed.port}")
        except ValueError:
            continue
    return origins


def trusted_origins(request):
    origins = configured_origins() | {str(request.base_url).rstrip("/")}
    # The opt-in tunnel helper, not request headers, controls this allowlist.
    # localhost.run rewrites Host; never trust arbitrary X-Forwarded-Host values.
    try:
        state = json.loads(
            (settings.storage_path / "INTEGRATION_STATUS.json").read_text()
        )
        if not isinstance(state, dict):
            return origins
        base = state.get("base_url", "")
        if state.get("tunnel_running") and re.fullmatch(
            r"https://[a-zA-Z0-9-]+\.lhr\.(life|rocks)", base
        ):
            origins.add(base)
    except (OSError, ValueError, TypeError):
        pass
    return origins


async def actor(request: Request) -> Actor:
    from app.db.session import SessionFactory
    from app.db.auth_tables import UserRecord, SessionRecord

    raw = request.cookies.get("modelrisk_session")
    if raw:
        async with SessionFactory() as session:
            saved = await session.get(
                SessionRecord, hashlib.sha256(raw.encode()).hexdigest()
            )
            if saved and saved.expires_at.replace(tzinfo=timezone.utc) > datetime.now(
                timezone.utc
            ):
                user = await session.get(UserRecord, saved.user_id)
                if user and user.active:
                    if request.method not in ("GET", "HEAD", "OPTIONS"):
                        origin = request.headers.get("origin")
                        allowed = trusted_origins(request)
                        if origin and origin not in allowed:
                            raise HTTPException(403, detail="Untrusted request origin")
                    return Actor(user.id, user.role)
        raise HTTPException(401, detail="Session expired; sign in again")
    if settings.allow_local_demo_auth:
        authorization = request.headers.get("authorization")
        if authorization is None:
            return Actor("local-operator", "operator")
        token = settings.reviewer_token
        if token and secrets.compare_digest(
            authorization, "Bearer " + token.get_secret_value()
        ):
            return Actor(settings.reviewer_id, "reviewer")
    raise HTTPException(401, detail="Sign in required")


async def reviewer(request: Request) -> Actor:
    who = await actor(request)
    if who.role not in ("validator", "approver", "reviewer", "admin"):
        raise HTTPException(403, detail="Independent validator, approver or admin required")
    return who


async def admin(request: Request) -> Actor:
    who = await actor(request)
    if who.role != "admin":
        raise HTTPException(403, detail="Administrator required")
    return who

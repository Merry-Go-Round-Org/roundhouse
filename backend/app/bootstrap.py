"""Provision local accounts without displaying credentials or changing existing users.
Run: python -m app.bootstrap --demo
"""

import argparse
import asyncio
import getpass
import secrets
import os
from uuid import uuid4
from sqlalchemy import select
from argon2 import PasswordHasher
from app.config import settings
from app.db.session import initialize_database, SessionFactory, engine

# Register all application tables, including uploads and chat.
from app.main import app  # noqa: F401
from app.db.auth_tables import UserRecord


async def main(demo=False):
    await initialize_database()
    hasher = PasswordHasher()
    users = []
    if demo:
        users = [
            (role, role, secrets.token_urlsafe(20))
            for role in ("admin", "developer", "validator", "approver")
        ]
    else:
        name = input("Administrator username: ").strip().casefold()
        password = getpass.getpass("Password (12+ characters): ")
        if not name or len(password) < 12:
            raise SystemExit("Username and 12+ character password required")
        users = [(name, "admin", password)]
    credentials = []
    async with SessionFactory() as session:
        for name, role, password in users:
            if await session.scalar(
                select(UserRecord.id).where(UserRecord.username == name)
            ):
                continue
            session.add(
                UserRecord(
                    id=str(uuid4()),
                    username=name,
                    role=role,
                    password_hash=await asyncio.to_thread(hasher.hash, password),
                )
            )
            credentials.append(f"{name}\t{role}\t{password}")
        if demo and credentials:
            path = settings.storage_path / "local-demo-credentials.txt"
            # Exclusive create prevents accidentally losing previously provisioned secrets.
            with os.fdopen(
                os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
            ) as stream:
                stream.write(
                    "LOCAL DEVELOPMENT ONLY — share roles privately, never commit this file.\nusername\trole\tpassword\n"
                    + "\n".join(credentials)
                    + "\n"
                )
        await session.commit()
    print(f"Created {len(credentials)} accounts. Existing accounts unchanged.")
    if demo and credentials:
        print(f"Private credentials saved to {path}")
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    asyncio.run(main(parser.parse_args().demo))

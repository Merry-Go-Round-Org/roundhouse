"""Environment configuration shared by API and worker."""

from pathlib import Path
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

FROZEN_SCORING_IMAGE = "ghcr.io/merry-go-round-org/sandbox@sha256:8083453253c3267caaa0fe36b43a287b9a986d5784148ad40714cd9d3fc2d80e"

BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")
    app_name: str = "Model Risk Factory API"
    api_prefix: str = "/api/v1"
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]
    )
    storage_dir: Path = BACKEND_DIR / "storage"
    legacy_models_root: Path = BACKEND_DIR.parent / "legacy_models"
    scoring_image: str | None = (
        None  # approved frozen registry digest or immutable local sha256 image ID
    )
    scoring_timeout_seconds: int = Field(default=120, ge=1, le=900)
    database_url: str | None = None
    run_timeout_seconds: int = Field(default=300, ge=1, le=86400)
    # Loaded in the trusted worker; never serialize into task arguments or sandbox env.
    openrouter_api_key: SecretStr | None = None
    reviewer_token: SecretStr | None = None
    reviewer_id: str = "demo-reviewer"
    harness_budget_usd: float = Field(default=1.0, gt=0, le=40)
    allow_local_demo_auth: bool = False
    allow_legacy_registration: bool = False
    ai_native_enabled: bool = True
    auth_cookie_secure: bool = False
    max_upload_bytes: int = 600 * 1024 * 1024
    redis_url: str = "redis://localhost:6379/0"

    @property
    def storage_path(self) -> Path:
        path = self.storage_dir
        return path.resolve() if path.is_absolute() else (BACKEND_DIR / path).resolve()

    @property
    def resolved_database_url(self) -> str:
        return (
            self.database_url or f"sqlite+aiosqlite:///{self.storage_path / 'app.db'}"
        )


settings = Settings()

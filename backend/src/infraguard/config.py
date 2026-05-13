"""Application settings loaded from environment + .env file."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


REPO_ROOT = Path(__file__).resolve().parents[3]
TERRAFORM_LAB_DIR = REPO_ROOT / "terraform-lab"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / "backend" / ".env",
        env_prefix="",
        extra="ignore",
    )

    anthropic_api_key: str | None = None
    infraguard_model: str = "claude-sonnet-4-6"
    infraguard_port: int = 8000
    infraguard_cors_origins: str = (
        "http://localhost:3000,"
        "http://localhost:3001,"
        "https://asbury.resume.miyagitrades.com"
    )

    # Real GitHub integration (Phase 3). When github_token is unset the runner
    # falls back to MockToolExecutor so the demo still works offline.
    github_token: str | None = None
    github_owner: str = "asellers3rd"
    github_repo: str = "infraguard-lab"
    github_default_branch: str = "main"

    @property
    def anthropic_configured(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def github_configured(self) -> bool:
        return bool(self.github_token)

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.infraguard_cors_origins.split(",") if origin.strip()]


settings = Settings()

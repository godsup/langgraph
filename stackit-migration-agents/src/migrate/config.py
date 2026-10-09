"""Runtime settings.

Secrets and environment-specific values come from environment variables or a
git-ignored `.env` file. Domain configuration (resource mapping, prices,
monitoring rules) lives in editable YAML files under `config/`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Preset for STACKIT AI Model Serving (OpenAI-compatible). Documented in the
# README; switching provider is a pure configuration change.
STACKIT_MODEL_SERVING_BASE_URL = (
    "https://api.openai-compat.model-serving.eu01.onstackit.cloud/v1"
)


class Settings(BaseSettings):
    """All runtime settings. Field names map 1:1 to upper-case env vars."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- LLM (any OpenAI-compatible endpoint) -----------------------------
    llm_model: str = "gpt-4o-mini"
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_temperature: float = 0.0
    llm_timeout_s: float = 120.0
    llm_structured_method: Literal["function_calling", "json_schema", "json_mode"] = (
        "function_calling"
    )

    # --- Paths ----------------------------------------------------------
    runs_dir: Path = PROJECT_ROOT / "runs"
    config_dir: Path = PROJECT_ROOT / "config"
    fixtures_dir: Path = PROJECT_ROOT / "fixtures"

    # --- Checkpointing --------------------------------------------------
    checkpoint_backend: Literal["sqlite", "postgres"] = "sqlite"
    checkpoint_sqlite_path: Path | None = None  # default: <runs_dir>/checkpoints.sqlite
    checkpoint_postgres_dsn: SecretStr | None = None

    # --- Approvals ------------------------------------------------------
    # Optional. When unset, a random per-process secret is used, so approval
    # tokens only live as long as the process that issued them.
    approval_secret: SecretStr | None = None
    approval_ttl_s: int = 900

    # --- Source: AWS (read-only credentials, standard AWS_* env vars) ------
    aws_region: str = "eu-central-1"
    aws_profile: str | None = None

    # --- Target: STACKIT --------------------------------------------------
    stackit_project_id: str | None = None
    stackit_region: str = "eu01"
    stackit_service_account_key_path: Path | None = None
    stackit_private_key_path: Path | None = None
    stackit_image_id: str | None = None  # Linux image UUID for migrated servers
    ssh_public_key_path: Path | None = None

    # --- Deployment: PR mode ---------------------------------------------
    pr_repo: str | None = None  # owner/name of the client's infrastructure repo
    pr_base_branch: str = "main"
    pr_repo_path: Path | None = None  # local clone used to push the branch

    # --- Data migration ----------------------------------------------------
    verify_sample_size: int = 20
    source_pg_dsn: SecretStr | None = None
    target_pg_dsn: SecretStr | None = None
    stackit_s3_endpoint: str = "https://object.storage.eu01.onstackit.cloud"
    stackit_s3_access_key_id: SecretStr | None = None
    stackit_s3_secret_access_key: SecretStr | None = None

    # --- Monitoring -------------------------------------------------------
    observability_url: str | None = None
    observability_username: SecretStr | None = None
    observability_password: SecretStr | None = None
    monitor_interval_s: int = 60
    webhook_url: SecretStr | None = None
    webhook_kind: Literal["teams", "slack"] = "slack"

    @field_validator(
        "llm_base_url",
        "pr_repo",
        "observability_url",
        "stackit_project_id",
        "stackit_image_id",
        mode="before",
    )
    @classmethod
    def _empty_is_none(cls, value: Any) -> Any:
        # `LLM_BASE_URL=` in .env means "use the OpenAI default".
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def sqlite_path(self) -> Path:
        return self.checkpoint_sqlite_path or self.runs_dir / "checkpoints.sqlite"

    def secret_values(self) -> list[str]:
        """Every configured secret value, used by the redactor."""
        values: list[str] = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                raw = value.get_secret_value()
                if raw:
                    values.append(raw)
        return values


@lru_cache
def get_settings() -> Settings:
    return Settings()


class YamlConfig:
    """Loads the editable domain configuration files from `config/`."""

    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir

    def load(self, name: str) -> dict[str, Any]:
        path = self.config_dir / name
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path} must contain a YAML mapping at the top level")
        return data

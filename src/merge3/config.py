"""Application configuration.

Settings come from environment variables (prefix ``MERGE3_``) with safe
local-only defaults — the service never needs a production account or an
external broker.  The configuration is isolated here rather than sprinkled
through the modules so tests can override it without touching the core.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MERGE3_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_path: str = Field(
        default="./data/merge3.db",
        description="SQLite database file; ':memory:' is allowed for tests.",
    )
    max_document_chars: int = Field(
        default=1_000_000,
        description="Reject documents larger than this many characters.",
    )
    log_redact_secrets: bool = Field(
        default=True,
        description="Redact sensitive-looking fields in diagnostics output.",
    )
    log_level: str = Field(default="INFO")
    retain_resolutions: bool = Field(
        default=True,
        description="Persist explicit conflict resolutions with the merge.",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test helper: force the next :func:`get_settings` to re-read the env."""
    get_settings.cache_clear()

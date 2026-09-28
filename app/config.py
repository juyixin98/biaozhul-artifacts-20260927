"""Service configuration.

Independent configuration layer: all tunables are explicit, validated once at
startup, and overridable through environment variables (prefix ``AUDIT_``) or a
local ``.env`` file. The cryptographic core never reads configuration
implicitly; policy values are passed in explicitly.
"""
from __future__ import annotations

import enum
import functools
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Protocol version is committed into every domain-separation label, so a
# mismatch across verifier/prover produces a concrete ROOT/COMMITMENT failure
# rather than silent cross-version acceptance.
PROTOCOL_VERSION = "audit-commit-v1"


class DigestAlgorithm(str, enum.Enum):
    """Only mature, audited hash functions are allowed."""

    SHA256 = "sha256"
    SHA384 = "sha384"
    SHA512 = "sha512"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AUDIT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_path: Path = Field(default=Path("./data/audit.db"))
    log_dir: Path = Field(default=Path("./logs"))
    log_level: str = Field(default="INFO")

    digest: DigestAlgorithm = Field(default=DigestAlgorithm.SHA256)
    default_salt_bytes: int = Field(default=16, ge=16, le=64)
    allow_unsalted: bool = Field(default=False)

    @field_validator("log_level")
    @classmethod
    def _normalise_level(cls, v: str) -> str:
        level = v.strip().upper()
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if level not in allowed:
            raise ValueError(f"unsupported log level: {v!r}")
        return level


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings (tests construct Settings explicitly)."""
    return Settings()  # type: ignore[call-arg]

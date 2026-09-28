"""Application settings, sourced from environment with local defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    policy_path: str
    fixture_db: str
    audit_db: str
    audit_key: str
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            policy_path=os.getenv(
                "SQLGUARD_POLICY", str(ROOT / "configs" / "policy.json")
            ),
            fixture_db=os.getenv(
                "SQLGUARD_FIXTURE", str(ROOT / "fixtures" / "shop.db")
            ),
            audit_db=os.getenv(
                "SQLGUARD_AUDIT_DB", str(ROOT / "runtime" / "audit.db")
            ),
            audit_key=os.getenv(
                "SQLGUARD_AUDIT_KEY", str(ROOT / "runtime" / "audit.key")
            ),
            host=os.getenv("SQLGUARD_HOST", "127.0.0.1"),
            port=int(os.getenv("SQLGUARD_PORT", "8080")),
        )

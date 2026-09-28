"""Application configuration (env + config file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Settings:
    policy_path: str
    fixture_dir: str
    audit_db_path: str
    audit_key_path: str
    host: str = "127.0.0.1"
    port: int = 8080
    log_level: str = "INFO"

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Settings":
        path = path or os.environ.get("SQLGUARD_CONFIG", "config/settings.yaml")
        data = {}
        p = Path(path)
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls(
            policy_path=os.environ.get("SQLGUARD_POLICY", data.get("policy_path", "config/policy.yaml")),
            fixture_dir=os.environ.get("SQLGUARD_FIXTURE", data.get("fixture_dir", "samples/fixture")),
            audit_db_path=os.environ.get("SQLGUARD_AUDIT_DB", data.get("audit_db_path", "data/audit.db")),
            audit_key_path=os.environ.get("SQLGUARD_AUDIT_KEY", data.get("audit_key_path", "data/audit.key")),
            host=os.environ.get("SQLGUARD_HOST", data.get("host", "127.0.0.1")),
            port=int(os.environ.get("SQLGUARD_PORT", data.get("port", 8080))),
            log_level=os.environ.get("SQLGUARD_LOG_LEVEL", data.get("log_level", "INFO")),
        )

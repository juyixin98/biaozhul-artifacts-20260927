"""全局配置。

全部可通过环境变量覆盖，默认值只指向本地临时/开发位置。
审计密钥用于加密存储的原文（Fernet），审计令牌用于访问原文接口。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_RULES_PATH = str(Path(__file__).resolve().parent.parent / "config" / "rules.json")
DEFAULT_DB_PATH = str(Path.cwd() / "var" / "audit.sqlite3")


def _default_audit_key() -> str:
    """开发用固定 Fernet 密钥（仅本地合成数据）。

    生产部署必须通过 LOG_REDACT_AUDIT_KEY 注入独立密钥。
    """
    return os.environ.get(
        "LOG_REDACT_AUDIT_KEY",
        "Zh3kN8vQ2pXsT7wYbF6hJdA1mCeRtUoPiLnKgVyXaQ0=",
    )


@dataclass(frozen=True)
class Settings:
    rules_path: str = os.environ.get("LOG_REDACT_RULES_PATH", DEFAULT_RULES_PATH)
    db_path: str = os.environ.get("LOG_REDACT_DB_PATH", DEFAULT_DB_PATH)
    audit_key: str = _default_audit_key()
    audit_token: str = os.environ.get("LOG_REDACT_AUDIT_TOKEN", "local-audit-token")
    max_request_chars: int = int(os.environ.get("LOG_REDACT_MAX_CHARS", "200000"))


def get_settings() -> Settings:
    return Settings()

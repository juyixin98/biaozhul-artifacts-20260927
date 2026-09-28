"""配置加载（与业务代码分离；环境变量优先，便于测试隔离）。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# 仅用于本地演示/测试的固定盐；生产场景主密钥应从 KMS / 环境注入。
_DEV_SALT = b"tss-local-dev-salt-v1"


@dataclass(frozen=True)
class Settings:
    master_key: bytes
    database_path: str
    audit_path: str
    env: str

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"


def _derive_key(passphrase: str) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=_DEV_SALT, iterations=200_000
    )
    return kdf.derive(passphrase.encode("utf-8"))


def load_settings(overrides: dict | None = None) -> Settings:
    """从环境变量读取配置；TSS_MASTER_KEY 可直接给 64 hex 字符。

    未配置时退化为开发态固定派生根——仅限本地，README 中明确警告。
    """
    overrides = overrides or {}
    env = overrides.get("env", os.getenv("TSS_ENV", "dev"))
    data_dir = overrides.get("data_dir", os.getenv("TSS_DATA_DIR", "./data"))
    Path(data_dir).mkdir(parents=True, exist_ok=True)

    raw_key = overrides.get("master_key", os.getenv("TSS_MASTER_KEY"))
    if raw_key:
        try:
            master_key = bytes.fromhex(raw_key)
            if len(master_key) != 32:
                raise ValueError
        except ValueError as exc:
            raise ValueError(
                "TSS_MASTER_KEY must be 32 bytes encoded as 64 hex characters"
            ) from exc
    elif env == "dev":
        master_key = _derive_key("local-development-passphrase")
    else:
        raise RuntimeError("TSS_MASTER_KEY is required outside of dev environment")

    return Settings(
        master_key=master_key,
        database_path=os.path.join(data_dir, "tss.db"),
        audit_path=os.path.join(data_dir, "audit.log"),
        env=env,
    )

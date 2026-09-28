"""配置：全部走环境变量，默认值面向本地合成环境。

任何配置都不指向真实业务系统；数据库默认落在本地文件，便于离线检查。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    db_path: str = os.environ.get("TEACHCHAIN_DB", "teachchain.db")
    host: str = os.environ.get("TEACHCHAIN_HOST", "127.0.0.1")
    port: int = _env_int("TEACHCHAIN_PORT", 8080)
    init_credit: int = _env_int("TEACHCHAIN_INIT_CREDIT", 10_000_000)
    log_level: str = os.environ.get("TEACHCHAIN_LOG_LEVEL", "INFO")


def get_settings() -> Settings:
    return Settings()

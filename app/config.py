"""配置:全部来自环境变量,均有本地默认值,不依赖任何生产账号。"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = "merge3.sqlite3"
    log_path: str = "diagnostics.log"
    local_label: str = "local"
    base_label: str = "base"
    remote_label: str = "remote"


def load_settings() -> Settings:
    env = os.environ
    return Settings(
        db_path=env.get("MERGE3_DB_PATH", Settings.db_path),
        log_path=env.get("MERGE3_LOG_PATH", Settings.log_path),
        local_label=env.get("MERGE3_LOCAL_LABEL", Settings.local_label),
        base_label=env.get("MERGE3_BASE_LABEL", Settings.base_label),
        remote_label=env.get("MERGE3_REMOTE_LABEL", Settings.remote_label),
    )

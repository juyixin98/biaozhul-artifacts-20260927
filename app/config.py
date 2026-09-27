"""运行期配置。

所有路径都可由环境变量覆盖，默认指向项目本地目录，
不依赖任何生产账号或外部服务。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    db_path: str = os.environ.get(
        "POSTING_DB_PATH",
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data", "index.sqlite3")),
    )
    log_path: str = os.environ.get(
        "POSTING_LOG_PATH",
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "logs", "service.log")),
    )
    block_size: int = int(os.environ.get("POSTING_BLOCK_SIZE", "8"))
    auto_seed: bool = os.environ.get("POSTING_AUTO_SEED", "1") != "0"
    trace_ring_size: int = int(os.environ.get("POSTING_TRACE_RING", "256"))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

"""配置层。

所有配置集中于此，服务入口（``app.main``）与测试都从这里取默认值，
也可以通过环境变量覆盖：

- ``CTRIE_DB_PATH``   SQLite 数据库文件路径
- ``CTRIE_TOPK_MAX``  单次查询允许的最大 k
- ``CTRIE_LOG_LEVEL`` 日志级别
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """不可变运行配置。"""

    db_path: str = "data/ctrie.db"
    topk_max: int = 100
    log_level: str = "INFO"

    @staticmethod
    def from_env() -> "Settings":
        """从环境变量读取配置，非法值直接抛 ``ValueError``（不静默吞掉）。"""
        topk_raw = os.environ.get("CTRIE_TOPK_MAX", "100")
        try:
            topk = int(topk_raw)
        except ValueError as exc:
            raise ValueError(f"CTRIE_TOPK_MAX 必须是整数，实际为 {topk_raw!r}") from exc
        if topk < 1:
            raise ValueError(f"CTRIE_TOPK_MAX 必须 >= 1，实际为 {topk}")
        level = os.environ.get("CTRIE_LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"CTRIE_LOG_LEVEL 非法: {level!r}")
        return Settings(
            db_path=os.environ.get("CTRIE_DB_PATH", "data/ctrie.db"),
            topk_max=topk,
            log_level=level,
        )


DEFAULT_SETTINGS = Settings()

"""全局配置：所有运行参数都来自环境变量，默认值保证开箱即用。

- POSTING_DB_PATH: SQLite 持久化文件
- POSTING_LOG_DIR: 结构化 JSONL 诊断日志目录
- POSTING_BLOCK_SIZE: posting 列表分块大小（跳跃块）
- POSTING_HOST / POSTING_PORT: uvicorn 监听地址
"""
from __future__ import annotations

import os
import pathlib

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent


class Settings:
    def __init__(
        self,
        db_path: str | None = None,
        log_dir: str | None = None,
        block_size: int | None = None,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        self.db_path = db_path or os.environ.get(
            "POSTING_DB_PATH", str(ROOT_DIR / "data" / "postings.db")
        )
        self.log_dir = log_dir or os.environ.get(
            "POSTING_LOG_DIR", str(ROOT_DIR / "logs")
        )
        self.block_size = block_size or int(os.environ.get("POSTING_BLOCK_SIZE", "8"))
        self.host = host or os.environ.get("POSTING_HOST", "127.0.0.1")
        self.port = port or int(os.environ.get("POSTING_PORT", "8000"))

    def ensure_dirs(self) -> None:
        pathlib.Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(self.log_dir).mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    """进程内单例配置。"""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings

"""配置层。

12-factor 风格：默认值适合本地演示，全部可通过环境变量覆盖（前缀 ``TRIE_``）。
配置独立成层，算法/存储代码不直接读环境变量，便于测试注入。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: Path
    snapshot_dir: Path
    log_dir: Path
    log_level: str
    max_limit: int
    max_surface_len: int
    max_batch: int

    @staticmethod
    def from_env(env: dict[str, str] | None = None) -> "Settings":
        env = env if env is not None else dict(os.environ)
        base = Path(env.get("TRIE_DATA_DIR", "./data"))
        return Settings(
            db_path=Path(env.get("TRIE_DB_PATH", str(base / "trie.db"))),
            snapshot_dir=Path(env.get("TRIE_SNAPSHOT_DIR", str(base / "snapshots"))),
            log_dir=Path(env.get("TRIE_LOG_DIR", "./logs")),
            log_level=env.get("TRIE_LOG_LEVEL", "INFO").upper(),
            max_limit=int(env.get("TRIE_MAX_LIMIT", "1000")),
            max_surface_len=int(env.get("TRIE_MAX_SURFACE_LEN", "512")),
            max_batch=int(env.get("TRIE_MAX_BATCH", "10000")),
        )

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

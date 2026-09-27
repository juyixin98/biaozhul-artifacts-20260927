"""配置层：所有路径与限制集中在此，支持环境变量覆盖。

环境变量：
- ``MP4TL_FIXTURES_DIR`` : 合成夹具目录（默认仓库 fixtures/generated）
- ``MP4TL_DB_PATH``      : 作业 SQLite 数据库路径
- ``MP4TL_ALLOWED_ROOT`` : 允许通过 API 读取的文件根目录（可多次，冒号分隔）
- ``MP4TL_MAX_FILE_BYTES``: 单文件大小上限
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    fixtures_dir: Path
    db_path: Path
    allowed_roots: tuple[Path, ...] = field(default_factory=tuple)
    max_file_bytes: int = 64 * 1024 * 1024

    def ensure_dirs(self) -> None:
        self.fixtures_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    fixtures_dir = Path(
        os.environ.get("MP4TL_FIXTURES_DIR", str(REPO_ROOT / "fixtures" / "generated"))
    )
    db_path = Path(
        os.environ.get("MP4TL_DB_PATH", str(REPO_ROOT / "data" / "jobs.sqlite3"))
    )
    roots_env = os.environ.get(
        "MP4TL_ALLOWED_ROOT",
        os.pathsep.join(
            [str(REPO_ROOT / "fixtures"), str(REPO_ROOT / "fixtures" / "generated")]
        ),
    )
    allowed_roots = tuple(
        Path(p).resolve() for p in roots_env.split(os.pathsep) if p.strip()
    )
    max_file_bytes = int(os.environ.get("MP4TL_MAX_FILE_BYTES", str(64 * 1024 * 1024)))
    return Settings(
        fixtures_dir=fixtures_dir.resolve(),
        db_path=db_path.resolve(),
        allowed_roots=allowed_roots,
        max_file_bytes=max_file_bytes,
    )

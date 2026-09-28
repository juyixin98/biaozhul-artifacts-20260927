"""进程配置：仓库路径、SQLite 路径、默认资源上限。

环境变量覆盖：
- RTDA_WAREHOUSE  数据仓库根目录（Parquet 与 metadata.db）
- RTDA_DB         显式指定 SQLite 路径（优先于仓库内默认路径）
表级上限可在建表时通过 config 逐表覆盖。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_LIMITS = {
    "max_columns": 100,
    "max_key_columns": 16,
    "max_files_per_snapshot": 1000,
    "max_delete_files_per_snapshot": 1000,
    "max_rows_per_data_file": 1_000_000,
    "max_rows_per_delete_file": 1_000_000,
}


@dataclass
class Config:
    warehouse_dir: Path
    db_path: Path
    default_limits: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_LIMITS))

    @classmethod
    def from_env(cls, base_dir: str | os.PathLike[str] | None = None) -> "Config":
        base = Path(base_dir or os.environ.get("RTDA_WAREHOUSE", ".warehouse")).resolve()
        db = os.environ.get("RTDA_DB")
        db_path = Path(db).resolve() if db else base / "metadata.db"
        return cls(warehouse_dir=base, db_path=db_path)

    def table_dir(self, table_id: str) -> Path:
        return self.warehouse_dir / "tables" / table_id

    def data_path(self, table_id: str, file_id: str) -> Path:
        return self.table_dir(table_id) / "data" / f"{file_id}.parquet"

    def delete_path(self, table_id: str, delete_file_id: str) -> Path:
        return self.table_dir(table_id) / "deletes" / f"{delete_file_id}.parquet"

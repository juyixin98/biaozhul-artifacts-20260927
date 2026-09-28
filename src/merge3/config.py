"""配置加载层。

独立的配置文件（config/config.yaml）+ 环境变量覆盖，供应用、测试、
脚本共享，避免在代码中硬编码存储位置。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "config.yaml"


@dataclass(frozen=True)
class StorageConfig:
    root_dir: Path
    db_filename: str = "metadata.db"
    parquet_dirname: str = "snapshots"
    log_dirname: str = "logs"

    @property
    def db_path(self) -> Path:
        return self.root_dir / self.db_filename

    @property
    def parquet_dir(self) -> Path:
        return self.root_dir / self.parquet_dirname

    @property
    def log_dir(self) -> Path:
        return self.root_dir / self.log_dirname


@dataclass(frozen=True)
class ApiConfig:
    host: str = "127.0.0.1"
    port: int = 8000


@dataclass(frozen=True)
class RunLogConfig:
    include_per_key_decisions: bool = True


@dataclass(frozen=True)
class AppConfig:
    storage: StorageConfig
    api: ApiConfig
    runlog: RunLogConfig


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    """从 YAML 加载配置。路径相对配置文件所在项目根解析。

    环境变量 MERGE3_CONFIG 可覆盖配置文件位置（测试与演示使用）。
    """
    config_path = Path(path or os.environ.get("MERGE3_CONFIG") or DEFAULT_CONFIG_PATH)
    raw: dict = {}
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    project_root = config_path.resolve().parent.parent
    storage_raw = raw.get("storage", {})
    root = Path(storage_raw.get("root_dir", "./data"))
    if not root.is_absolute():
        root = project_root / root

    api_raw = raw.get("api", {})
    log_raw = raw.get("runlog", {})
    return AppConfig(
        storage=StorageConfig(
            root_dir=root.resolve(),
            db_filename=storage_raw.get("db_filename", "metadata.db"),
            parquet_dirname=storage_raw.get("parquet_dirname", "snapshots"),
            log_dirname=storage_raw.get("log_dirname", "logs"),
        ),
        api=ApiConfig(host=api_raw.get("host", "127.0.0.1"), port=int(api_raw.get("port", 8000))),
        runlog=RunLogConfig(
            include_per_key_decisions=bool(log_raw.get("include_per_key_decisions", True))
        ),
    )

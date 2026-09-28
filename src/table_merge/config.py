"""配置层：从 YAML 读取存储路径、服务端口与内核开关，环境变量可覆盖。

覆盖优先级：进程环境变量 > YAML 文件 > 内置默认值。
独立配置层使测试可以指向临时目录而不污染开发数据。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG: dict = {
    "storage": {
        "root_dir": "./data/store",   # SQLite 元数据库与 Parquet 快照根目录
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8000,
    },
    "kernel": {
        # 提交时是否强制校验待写入快照与父快照 schema 一致
        "strict_schema": True,
        # 单次计划允许的最大冲突行数（防止误传巨表）
        "max_conflicts": 100_000,
    },
    "logging": {
        "level": "INFO",
        "file": None,                  # None 表示只输出到 stderr
    },
}


@dataclass(frozen=True)
class AppConfig:
    storage_root: Path
    host: str
    port: int
    strict_schema: bool
    max_conflicts: int
    log_level: str
    log_file: str | None

    @property
    def db_path(self) -> Path:
        return self.storage_root / "metadata.sqlite3"

    @property
    def snapshot_dir(self) -> Path:
        return self.storage_root / "snapshots"


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _env_overrides(raw: dict) -> dict:
    """识别 TABLE_MERGE_ 前缀的环境变量，按双下划线分段映射。

    例：TABLE_MERGE_STORAGE__ROOT_DIR=/tmp/x -> storage.root_dir
    """
    prefix = "TABLE_MERGE_"
    for name, value in os.environ.items():
        if not name.startswith(prefix):
            continue
        path = name[len(prefix):].lower().split("__")
        cursor = raw
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[path[-1]] = value
    return raw


def load_config(path: str | os.PathLike | None = None) -> AppConfig:
    raw = {k: dict(v) if isinstance(v, dict) else v for k, v in DEFAULT_CONFIG.items()}
    if path:
        p = Path(path)
        if p.exists():
            with p.open("r", encoding="utf-8") as fh:
                raw = _deep_merge(raw, yaml.safe_load(fh) or {})
    raw = _env_overrides(raw)

    root = Path(raw["storage"]["root_dir"]).expanduser().resolve()
    return AppConfig(
        storage_root=root,
        host=str(raw["server"]["host"]),
        port=int(raw["server"]["port"]),
        strict_schema=bool(raw["kernel"]["strict_schema"]),
        max_conflicts=int(raw["kernel"]["max_conflicts"]),
        log_level=str(raw["logging"]["level"]).upper(),
        log_file=raw["logging"].get("file"),
    )

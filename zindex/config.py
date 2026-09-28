"""Configuration loading (JSON files under configs/).

Defaults are overridable by environment variables ``ZINDEX_*`` which is handy
for the test suite (pointing at a temporary data dir).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULTS: dict[str, object] = {
    "data_dir": "data",
    "catalog_path": "data/catalog.sqlite",
    "log_file": "data/zindex.log",
    "log_level": "INFO",
    "default_chunk_capacity": 8192,
    "default_max_intervals": 4096,
}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    catalog_path: Path
    log_file: Path
    log_level: str
    default_chunk_capacity: int
    default_max_intervals: int

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Settings":
        raw = dict(DEFAULTS)
        if path and Path(path).exists():
            with open(path, "r", encoding="utf-8") as fh:
                raw.update(json.load(fh))
        env_map = {
            "ZINDEX_DATA_DIR": ("data_dir", str),
            "ZINDEX_CATALOG_PATH": ("catalog_path", str),
            "ZINDEX_LOG_FILE": ("log_file", str),
            "ZINDEX_LOG_LEVEL": ("log_level", str),
            "ZINDEX_CHUNK_CAPACITY": ("default_chunk_capacity", int),
            "ZINDEX_MAX_INTERVALS": ("default_max_intervals", int),
        }
        for env, (key, cast) in env_map.items():
            if env in os.environ:
                raw[key] = cast(os.environ[env])
        return cls(
            data_dir=Path(raw["data_dir"]),
            catalog_path=Path(raw["catalog_path"]),
            log_file=Path(raw["log_file"]),
            log_level=str(raw["log_level"]),
            default_chunk_capacity=int(raw["default_chunk_capacity"]),
            default_max_intervals=int(raw["default_max_intervals"]),
        )

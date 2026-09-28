"""JSON configuration loading.

Resolution is explicit and documented: ``ZCLUSTER_CONFIG`` points at a JSON
file (default ``config/default.json``); ``data_root`` inside it is resolved
relative to the current working directory unless absolute.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("config/default.json")


@dataclass(frozen=True)
class Config:
    data_root: str
    chunk_size: int
    default_interval_budget: int
    max_interval_budget: int
    arrow_compression: str
    code_uint64_when_fit: bool
    log_level: str
    log_file: str
    source_path: str

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Config":
        cfg_path = Path(os.environ.get("ZCLUSTER_CONFIG", path or DEFAULT_CONFIG_PATH))
        with open(cfg_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        q = raw.get("query", {})
        s = raw.get("storage", {})
        lg = raw.get("logging", {})
        chunk_size = int(raw.get("chunk_size", 5000))
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")
        return cls(
            data_root=os.path.abspath(raw.get("data_root", "data")),
            chunk_size=chunk_size,
            default_interval_budget=int(q.get("default_interval_budget", 256)),
            max_interval_budget=int(q.get("max_interval_budget", 1_000_000)),
            arrow_compression=str(s.get("arrow_compression", "zstd")),
            code_uint64_when_fit=bool(s.get("code_uint64_when_fit", True)),
            log_level=str(lg.get("level", "INFO")),
            log_file=str(lg.get("file", "logs/zcluster.log")),
            source_path=str(cfg_path.resolve()),
        )

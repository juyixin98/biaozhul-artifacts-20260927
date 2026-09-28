"""Configuration loading.

Resolution order: explicit arguments > environment variables > config JSON >
built-in defaults.  Everything is local (filesystem paths and numeric caps);
there are no remote accounts or credentials.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .universe import DEFAULT_SPACE_CAP

DEFAULT_DATA_DIR = "./.osdiff-data"
DEFAULT_DB_NAME = "osdiff.sqlite3"
DEFAULT_KEY_NAME = "audit_ed25519.pem"


@dataclass
class Config:
    data_dir: str = DEFAULT_DATA_DIR
    db_path: str = ""
    key_path: str = ""
    space_cap: int = DEFAULT_SPACE_CAP
    witness_limit_per_category: int = 5

    def __post_init__(self) -> None:
        if not self.db_path:
            self.db_path = str(Path(self.data_dir) / DEFAULT_DB_NAME)
        if not self.key_path:
            self.key_path = str(Path(self.data_dir) / DEFAULT_KEY_NAME)

    def to_dict(self) -> dict:
        return asdict(self)


def load_config(path: str | os.PathLike[str] | None = None, **overrides: object) -> Config:
    data: dict[str, object] = {}
    if path and Path(path).exists():
        data.update(json.loads(Path(path).read_text(encoding="utf-8")))

    env_map = {
        "data_dir": "OSDIFF_DATA_DIR",
        "db_path": "OSDIFF_DB_PATH",
        "key_path": "OSDIFF_KEY_PATH",
        "space_cap": "OSDIFF_SPACE_CAP",
        "witness_limit_per_category": "OSDIFF_WITNESS_LIMIT",
    }
    for field, env in env_map.items():
        if env in os.environ:
            data[field] = os.environ[env]

    data.update({k: v for k, v in overrides.items() if v is not None})

    if "space_cap" in data:
        data["space_cap"] = int(data["space_cap"])
    if "witness_limit_per_category" in data:
        data["witness_limit_per_category"] = int(data["witness_limit_per_category"])

    allowed = {f for f in Config.__dataclass_fields__}  # type: ignore[attr-defined]
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    return Config(**data)  # type: ignore[arg-type]

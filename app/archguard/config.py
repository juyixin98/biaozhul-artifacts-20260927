"""Configuration loading.

Precedence (highest first):

1. ``ARCHGUARD_*`` environment variables
2. JSON file given via ``--config`` / ``ARCHGUARD_CONFIG``
3. ``config.json`` next to the working directory, if present
4. built-in defaults
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import RejectionCategory, RejectionError

DEFAULTS: dict = {
    "home": "./var",
    "max_total_bytes": 10 * 1024 * 1024,
    "max_files": 1000,
    "max_depth": 16,
    "max_symlink_hops": 8,
    "max_compression_ratio": 100.0,
    "max_upload_bytes": 50 * 1024 * 1024,
    "keep_failed_dirs": False,
    "host": "127.0.0.1",
    "port": 8080,
}

_ENV_MAP = {
    "ARCHGUARD_HOME": ("home", str),
    "ARCHGUARD_MAX_TOTAL_BYTES": ("max_total_bytes", int),
    "ARCHGUARD_MAX_FILES": ("max_files", int),
    "ARCHGUARD_MAX_DEPTH": ("max_depth", int),
    "ARCHGUARD_MAX_SYMLINK_HOPS": ("max_symlink_hops", int),
    "ARCHGUARD_MAX_COMPRESSION_RATIO": ("max_compression_ratio", float),
    "ARCHGUARD_MAX_UPLOAD_BYTES": ("max_upload_bytes", int),
    "ARCHGUARD_KEEP_FAILED_DIRS": ("keep_failed_dirs", lambda v: v == "1"),
    "ARCHGUARD_HOST": ("host", str),
    "ARCHGUARD_PORT": ("port", int),
}


@dataclass(frozen=True)
class Config:
    home: Path
    max_total_bytes: int
    max_files: int
    max_depth: int
    max_symlink_hops: int
    max_compression_ratio: float
    max_upload_bytes: int
    keep_failed_dirs: bool
    host: str
    port: int

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Config":
        data = dict(DEFAULTS)

        config_path = (
            Path(path)
            if path
            else Path(os.environ.get("ARCHGUARD_CONFIG", "config.json"))
        )
        if config_path.exists():
            try:
                loaded = json.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RejectionError(
                    RejectionCategory.INTERNAL_ERROR,
                    f"cannot read config {config_path}: {exc}",
                ) from exc
            unknown = set(loaded) - set(DEFAULTS)
            if unknown:
                raise RejectionError(
                    RejectionCategory.INTERNAL_ERROR,
                    f"unknown config keys: {sorted(unknown)}",
                )
            data.update(loaded)

        for env_name, (key, cast) in _ENV_MAP.items():
            if env_name in os.environ:
                data[key] = cast(os.environ[env_name])

        cfg = cls(
            home=Path(data["home"]).expanduser().resolve(),
            max_total_bytes=data["max_total_bytes"],
            max_files=data["max_files"],
            max_depth=data["max_depth"],
            max_symlink_hops=data["max_symlink_hops"],
            max_compression_ratio=data["max_compression_ratio"],
            port=data["port"],
            host=data["host"],
            max_upload_bytes=data["max_upload_bytes"],
            keep_failed_dirs=bool(data["keep_failed_dirs"]),
        )
        cfg._validate()
        return cfg

    def _validate(self) -> None:
        for name in (
            "max_total_bytes",
            "max_files",
            "max_depth",
            "max_symlink_hops",
            "max_upload_bytes",
        ):
            if getattr(self, name) <= 0:
                raise RejectionError(
                    RejectionCategory.INTERNAL_ERROR,
                    f"config {name} must be positive",
                )
        if self.max_compression_ratio <= 0:
            raise RejectionError(
                RejectionCategory.INTERNAL_ERROR,
                "max_compression_ratio must be positive",
            )
        if not self.host or not (1 <= self.port <= 65535):
            raise RejectionError(
                RejectionCategory.INTERNAL_ERROR,
                "invalid host/port in configuration",
            )

    def budget_limits(self) -> dict:
        return {
            "max_total_bytes": self.max_total_bytes,
            "max_files": self.max_files,
            "max_depth": self.max_depth,
            "max_symlink_hops": self.max_symlink_hops,
            "max_compression_ratio": self.max_compression_ratio,
        }

    def to_dict(self) -> dict:
        out = asdict(self)
        out["home"] = str(self.home)
        return out

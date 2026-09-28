"""Configuration layer.

Loaded from a JSON file (config/default.json by default), with every value
overridable by an environment variable using the ARCHIVEGUARD_ prefix.
Nothing here touches production services; paths are local and relative to the
project root.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # .../b
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "default.json"


@dataclass(frozen=True)
class Budgets:
    max_total_uncompressed_bytes: int = 64 * 1024 * 1024
    max_file_size_bytes: int = 16 * 1024 * 1024
    max_entries: int = 500
    max_depth: int = 20
    max_compression_ratio: int = 100
    symlink_resolution_steps: int = 40


@dataclass(frozen=True)
class Policy:
    allow_case_collisions: bool = False
    allow_symlinks: bool = True


@dataclass(frozen=True)
class Settings:
    version: str
    workspace_root: Path
    audit_db: Path
    max_upload_bytes: int
    budgets: Budgets = field(default_factory=Budgets)
    policy: Policy = field(default_factory=Policy)
    config_path: Path | None = None

    def to_json_dict(self) -> dict:
        data = asdict(self)
        data["workspace_root"] = str(self.workspace_root)
        data["audit_db"] = str(self.audit_db)
        data["config_path"] = str(self.config_path) if self.config_path else None
        return data


def _env(name: str) -> str | None:
    return os.environ.get(f"ARCHIVEGUARD_{name}")


def _resolve(path_value: str) -> Path:
    """Relative paths resolve against the current working directory (which the
    runbook sets to the project root); absolute paths are taken verbatim."""
    p = Path(path_value)
    return p if p.is_absolute() else (Path.cwd() / p).resolve()


def load_settings(config_path: str | Path | None = None) -> Settings:
    """Load settings; JSON file first, then environment overrides.

    Environment override keys (all ints unless noted):
      ARCHIVEGUARD_WORKSPACE_ROOT, ARCHIVEGUARD_AUDIT_DB,
      ARCHIVEGUARD_MAX_UPLOAD_BYTES,
      ARCHIVEGUARD_MAX_TOTAL_UNCOMPRESSED_BYTES, ARCHIVEGUARD_MAX_FILE_SIZE_BYTES,
      ARCHIVEGUARD_MAX_ENTRIES, ARCHIVEGUARD_MAX_DEPTH,
      ARCHIVEGUARD_MAX_COMPRESSION_RATIO, ARCHIVEGUARD_SYMLINK_RESOLUTION_STEPS,
      ARCHIVEGUARD_ALLOW_CASE_COLLISIONS (bool), ARCHIVEGUARD_ALLOW_SYMLINKS (bool).
    """
    path = Path(config_path) if config_path else Path(
        _env("CONFIG_PATH") or DEFAULT_CONFIG_PATH
    )
    raw = json.loads(path.read_text(encoding="utf-8"))

    b = dict(raw.get("budgets", {}))
    p = dict(raw.get("policy", {}))

    def apply_int(section: dict, key_env: str, key: str) -> None:
        v = _env(key_env)
        if v is not None:
            section[key] = int(v)

    def apply_bool(section: dict, key_env: str, key: str) -> None:
        v = _env(key_env)
        if v is not None:
            section[key] = v.strip().lower() in {"1", "true", "yes", "on"}

    if _env("WORKSPACE_ROOT"):
        raw["workspace_root"] = _env("WORKSPACE_ROOT")
    if _env("AUDIT_DB"):
        raw["audit_db"] = _env("AUDIT_DB")
    apply_int(raw, "MAX_UPLOAD_BYTES", "max_upload_bytes")
    for env_name, key in (
        ("MAX_TOTAL_UNCOMPRESSED_BYTES", "max_total_uncompressed_bytes"),
        ("MAX_FILE_SIZE_BYTES", "max_file_size_bytes"),
        ("MAX_ENTRIES", "max_entries"),
        ("MAX_DEPTH", "max_depth"),
        ("MAX_COMPRESSION_RATIO", "max_compression_ratio"),
        ("SYMLINK_RESOLUTION_STEPS", "symlink_resolution_steps"),
    ):
        apply_int(b, env_name, key)
    apply_bool(p, "ALLOW_CASE_COLLISIONS", "allow_case_collisions")
    apply_bool(p, "ALLOW_SYMLINKS", "allow_symlinks")

    return Settings(
        version=raw["version"],
        workspace_root=_resolve(raw["workspace_root"]),
        audit_db=_resolve(raw["audit_db"]),
        max_upload_bytes=int(raw["max_upload_bytes"]),
        budgets=Budgets(**b),
        policy=Policy(**p),
        config_path=path.resolve(),
    )

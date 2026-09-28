"""Application configuration.

Configuration is isolated from runtime code: every path and policy knob is
resolved here, from a JSON file or environment variables, never hard-coded
inside the transaction kernel.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


DEFAULT_CONFIG: dict[str, Any] = {
    # Root directory for published parquet files (the "lake table" storage).
    "warehouse_dir": "warehouse",
    # SQLite database holding snapshots / manifest rows / cleanup ledger.
    "db_path": "run_results/metadata.sqlite3",
    # Staging directory for fully-written-but-unpublished files.
    "staging_dir": "run_results/staging",
    # Prefix of the orphan directory planted by the orphan-fixture scenario.
    "orphan_prefix": "_orphan_demo",
    # Concurrency policy.
    "max_retry_attempts": 3,
    # Diagnostic verbosity: when true, diagnostics may include absolute paths.
    # Sensitive *values* are always redacted; this only toggles path detail.
    "log_full_paths": False,
}

# Environment variable override map: ENV_VAR -> config key
_ENV_OVERRIDES = {
    "LAKE_WAREHOUSE_DIR": "warehouse_dir",
    "LAKE_DB_PATH": "db_path",
    "LAKE_STAGING_DIR": "staging_dir",
    "LAKE_ORPHAN_PREFIX": "orphan_prefix",
    "LAKE_MAX_RETRY_ATTEMPTS": "max_retry_attempts",
    "LAKE_LOG_FULL_PATHS": "log_full_paths",
}


@dataclass(frozen=True)
class Settings:
    warehouse_dir: Path
    db_path: Path
    staging_dir: Path
    orphan_prefix: str = "_orphan_demo"
    max_retry_attempts: int = 3
    log_full_paths: bool = False
    base_dir: Path = field(default_factory=Path.cwd)

    def describe(self) -> dict[str, Any]:
        """Safe (redacted) view of settings for diagnostics."""
        return {
            "warehouse_dir": str(self.warehouse_dir),
            "db_path": str(self.db_path),
            "staging_dir": str(self.staging_dir),
            "orphan_prefix": self.orphan_prefix,
            "max_retry_attempts": self.max_retry_attempts,
        }


def _coerce(key: str, raw: str) -> Any:
    if key == "max_retry_attempts":
        return int(raw)
    if key == "log_full_paths":
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return raw


def load_settings(config_path: str | os.PathLike[str] | None = None) -> Settings:
    """Load settings with precedence: env vars > config file > defaults."""
    raw: dict[str, Any] = dict(DEFAULT_CONFIG)

    if config_path is not None:
        p = Path(config_path)
        if p.exists():
            file_cfg = json.loads(p.read_text(encoding="utf-8"))
            raw.update(file_cfg)

    for env_name, key in _ENV_OVERRIDES.items():
        if env_name in os.environ:
            raw[key] = _coerce(key, os.environ[env_name])

    base_dir = Path.cwd()
    warehouse_dir = Path(raw["warehouse_dir"])
    db_path = Path(raw["db_path"])
    staging_dir = Path(raw["staging_dir"])
    if not warehouse_dir.is_absolute():
        warehouse_dir = base_dir / warehouse_dir
    if not db_path.is_absolute():
        db_path = base_dir / db_path
    if not staging_dir.is_absolute():
        staging_dir = base_dir / staging_dir

    return Settings(
        warehouse_dir=warehouse_dir,
        db_path=db_path,
        staging_dir=staging_dir,
        orphan_prefix=str(raw["orphan_prefix"]),
        max_retry_attempts=int(raw["max_retry_attempts"]),
        log_full_paths=bool(raw["log_full_paths"]),
        base_dir=base_dir,
    )

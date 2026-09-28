"""Configuration layer.

Values are read from ``config/default.toml`` and may be overridden by ``APP_*``
environment variables (e.g. ``APP_PORT=9000``). Keeping configuration separate
from the kernels lets tests point at their own throwaway SQLite database.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "default.toml"


@dataclass(frozen=True)
class Settings:
    app_name: str
    host: str
    port: int
    database_path: str
    log_level: str
    log_format: str
    preview_limit: int

    def resolved_db_path(self) -> Path:
        p = Path(self.database_path)
        return p if p.is_absolute() else REPO_ROOT / p


def _coerce(key: str, raw: str, current: object) -> object:
    if isinstance(current, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int) and not isinstance(current, bool):
        return int(raw)
    return raw


def load_settings(config_path: str | os.PathLike[str] | None = None) -> Settings:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    # Environment overrides: APP_<UPPER_KEY>.
    for key in list(data.keys()):
        env_key = f"APP_{key.upper()}"
        if env_key in os.environ:
            data[key] = _coerce(key, os.environ[env_key], data[key])
    return Settings(
        app_name=str(data["app_name"]),
        host=str(data["host"]),
        port=int(data["port"]),
        database_path=str(data["database_path"]),
        log_level=str(data["log_level"]).upper(),
        log_format=str(data["log_format"]),
        preview_limit=int(data["preview_limit"]),
    )

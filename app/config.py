"""Configuration layer.  All settings come from environment variables with
safe local defaults; nothing here requires production credentials."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from app import __version__

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    app_version: str = __version__
    db_path: str = str(PROJECT_ROOT / "var" / "jobs.db")
    log_dir: str = str(PROJECT_ROOT / "var" / "logs")
    fixture_dir: str = str(PROJECT_ROOT / "fixtures" / "data")
    container: str = "mp4-constrained"
    # constrained-container rules, versioned so plans can state what they
    # were checked against
    container_ruleset: str = "mp4-constrained/v1"

    def ensure_dirs(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.log_dir).mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        db_path=os.environ.get("APP_DB_PATH", Settings.db_path),
        log_dir=os.environ.get("APP_LOG_DIR", Settings.log_dir),
        fixture_dir=os.environ.get("APP_FIXTURE_DIR", Settings.fixture_dir),
        container=os.environ.get("APP_CONTAINER", Settings.container),
    )


def dependency_versions() -> dict[str, str]:
    import platform

    import fastapi
    import numpy
    import pydantic

    return {
        "app": __version__,
        "python": platform.python_version(),
        "numpy": numpy.__version__,
        "fastapi": fastapi.__version__,
        "pydantic": pydantic.__version__,
    }

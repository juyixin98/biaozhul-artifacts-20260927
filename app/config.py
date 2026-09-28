"""Service configuration. Defaults are local-only; override via SUBVAL_* env vars."""
import os
from dataclasses import dataclass


@dataclass
class Settings:
    db_path: str = "./data/subval.db"
    # Maximum number of grid points the exact solver may allocate per axis.
    max_grid: int = 5000

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=os.environ.get("SUBVAL_DB_PATH", cls.db_path),
            max_grid=int(os.environ.get("SUBVAL_MAX_GRID", cls.max_grid)),
        )

"""Application assembly: construct the store, engine and diagnostics together."""
from __future__ import annotations

from pathlib import Path

from .config import Settings
from .diag.logger import Diagnostics
from .kernel.engine import ChainEngine
from .storage.store import IndexStore


class Application:
    def __init__(self, settings: Settings, authorized_producers: set[str]):
        Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self.store = IndexStore(settings.database_path)
        self.diag = Diagnostics(self.store, log_level=settings.log_level)
        self.engine = ChainEngine(
            self.store,
            self.diag,
            allowed_difficulties=settings.allowed_difficulties,
            finality_depth=settings.finality_depth,
            authorized_producers=authorized_producers,
        )

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "Application":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

"""Composition root: wire settings -> kernel services.

Kept separate from the HTTP layer so tests and scripts can build the same
object graph without starting a server.
"""
from __future__ import annotations

from pathlib import Path

from app.kernel.metadata import MetadataStore
from app.kernel.storage import CleanupLedger, FileStorage
from app.services.commits import TableService
from app.services.diagnostics import Diagnostics
from config.settings import Settings, load_settings


class Container:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = MetadataStore(settings.db_path)
        self.ledger = CleanupLedger(settings.db_path.with_name("cleanup.sqlite3"))
        self.storage = FileStorage(settings.warehouse_dir, settings.staging_dir)
        self.diagnostics = Diagnostics(
            settings.db_path.parent / "diagnostics.jsonl"
        )
        self.service = TableService(
            store=self.store,
            storage=self.storage,
            ledger=self.ledger,
            diagnostics=self.diagnostics,
            max_attempts=settings.max_retry_attempts,
        )

    def reset_storage_dirs(self) -> None:
        """Test helper: clear warehouse / staging / result files."""
        import shutil

        for d in (self.settings.warehouse_dir, self.settings.staging_dir):
            shutil.rmtree(d, ignore_errors=True)
            Path(d).mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        self.store.close()
        self.ledger.close()


def build_container(config_path: str | None = None) -> Container:
    return Container(load_settings(config_path))

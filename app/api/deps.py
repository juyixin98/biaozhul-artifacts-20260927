"""Application container and FastAPI dependency wiring."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from ..config import Settings
from ..diagnostics import Recorder, is_safe_request_id, new_request_id
from ..storage.db import Database
from ..storage.diag_repo import DiagnosticRepo
from ..storage.scan_repo import ScanRepo
from ..storage.version_repo import VersionRepo
from ..services.scans import ScanService
from ..services.versions import VersionService


@dataclass
class Container:
    settings: Settings
    db: Database
    version_repo: VersionRepo
    scan_repo: ScanRepo
    diag_repo: DiagnosticRepo
    versions: VersionService
    scans: ScanService

    @classmethod
    def build(cls, settings: Settings) -> "Container":
        db = Database(settings.absolute_db_path())
        version_repo = VersionRepo(db)
        scan_repo = ScanRepo(db)
        diag_repo = DiagnosticRepo(db)
        versions = VersionService(
            version_repo,
            max_patterns=settings.max_patterns,
            max_pattern_bytes=settings.max_pattern_bytes,
        )
        scans = ScanService(
            scan_repo,
            versions,
            secret=settings.secret,
            default_page_limit=settings.default_page_limit,
            max_page_limit=settings.max_page_limit,
            max_chunk_bytes=settings.max_chunk_bytes,
        )
        return cls(
            settings=settings,
            db=db,
            version_repo=version_repo,
            scan_repo=scan_repo,
            diag_repo=diag_repo,
            versions=versions,
            scans=scans,
        )

    def shutdown(self) -> None:
        self.db.close()


def get_container(request: Request) -> Container:
    return request.app.state.container


def get_recorder(request: Request) -> Recorder:
    """Per-request recorder; created by the diagnostic middleware."""
    return request.state.recorder


def resolve_request_id(request: Request) -> str:
    supplied = request.headers.get("x-request-id")
    if is_safe_request_id(supplied):
        return supplied  # type: ignore[return-value]
    return new_request_id()

"""FastAPI validation interface.

Thin HTTP wrapper around :class:`TableService`.  The interesting logic
(conflict detection, atomic publish, cleanup ledger, diagnostics) lives in
the kernel/services modules; this module only maps requests/responses and
translates domain errors to stable JSON error bodies carrying request ids.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api_models import (
    CleanupRecordView,
    CommitLogView,
    CommitRequest,
    CommitResponse,
    CreateTableRequest,
    FileView,
    SnapshotView,
)
from app.container import Container, build_container
from app.kernel.errors import ErrorCategory, ServiceError, new_request_id

_CONTAINER: Container | None = None


def get_container() -> Container:
    global _CONTAINER
    if _CONTAINER is None:
        _CONTAINER = build_container()
    return _CONTAINER


def set_container(container: Container) -> None:
    global _CONTAINER
    _CONTAINER = container


def create_app(container: Container | None = None) -> FastAPI:
    if container is not None:
        set_container(container)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Clear PENDING commits from a crashed previous process on startup.
        get_container().service.recover_pending()
        yield

    app = FastAPI(
        title="Simplified Lake-Table Metadata Transaction Service",
        version="0.1.0",
        description=(
            "Local synthetic-fixture service. Implements snapshot-based "
            "commits, disjoint-partition append rebasing and hard conflict "
            "detection for overlapping overwrites. **This is not a full "
            "Iceberg implementation.**"
        ),
        lifespan=lifespan,
    )

    # -------------------------------------------------------------- errors

    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError) -> JSONResponse:
        status = _HTTP_STATUS.get(exc.category, 500)
        return JSONResponse(status_code=status, content=exc.to_dict())

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # -------------------------------------------------------------- tables

    @app.post("/tables", status_code=201)
    async def create_table(req: CreateTableRequest) -> dict:
        return get_container().service.create_table(
            req.table, req.partition_spec
        )

    # -------------------------------------------------------------- commits

    @app.post("/commits", response_model=CommitResponse, status_code=201)
    async def commit(req: CommitRequest) -> dict:
        result = get_container().service.commit(
            table_name=req.table,
            operation=req.operation,
            request_id=req.request_id,
            source_paths=req.files,
            base_snapshot_id=req.base_snapshot_id,
        )
        return result.to_dict()

    # ------------------------------------------------------------- snapshots

    def _snapshot_payload(table: str, snapshot_id: int) -> dict:
        c = get_container()
        snap = c.store.get_snapshot(table, snapshot_id)
        entries = c.store.snapshot_entries(snapshot_id)
        return {
            "snapshot_id": snapshot_id,
            "parent_snapshot_id": snap["parent_snapshot_id"],
            "operation": snap["operation"],
            "commit_id": snap["commit_id"],
            "created_at": snap["created_at"],
            "row_count": sum(e.row_count for e in entries),
            "files": [
                FileView(
                    relpath=e.file_relpath,
                    source=e.file_relpath.split("/")[-1],
                    fingerprint="",
                    row_count=e.row_count,
                    partition_keys=list(e.partition_keys),
                ).model_dump()
                for e in entries
            ],
        }

    @app.get("/tables/{table}/snapshots/latest", response_model=SnapshotView)
    async def latest_snapshot(table: str) -> dict:
        c = get_container()
        head = c.store.head_snapshot_id(table)
        return _snapshot_payload(table, head)

    @app.get(
        "/tables/{table}/snapshots/{snapshot_id}", response_model=SnapshotView
    )
    async def get_snapshot(table: str, snapshot_id: int) -> dict:
        return _snapshot_payload(table, snapshot_id)

    @app.get("/tables/{table}/snapshots")
    async def list_snapshots(table: str) -> dict:
        c = get_container()
        rows = c.store.list_snapshots(table)
        return {
            "table": table,
            "snapshots": [
                {
                    "snapshot_id": r["snapshot_id"],
                    "parent_snapshot_id": r["parent_snapshot_id"],
                    "operation": r["operation"],
                    "commit_id": r["commit_id"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
        }

    # --------------------------------------------------------------- commits

    @app.get("/commits", response_model=list[CommitLogView])
    async def list_commits(table: str | None = None) -> list[dict]:
        c = get_container()
        return [
            CommitLogView(
                request_id=r.request_id,
                table=r.table_name,
                operation=r.operation,
                status=r.status,
                base_snapshot_id=r.base_snapshot_id,
                final_snapshot_id=r.final_snapshot_id,
                rebased=r.rebased,
                attempts=r.attempts,
                error_category=r.error_category,
                error_message=r.error_message,
            ).model_dump()
            for r in c.store.list_commits(table)
        ]

    @app.get("/commits/{request_id}")
    async def get_commit(request_id: str) -> dict:
        c = get_container()
        record = c.store.get_commit(request_id)
        if record is None:
            raise ServiceError(
                ErrorCategory.NOT_FOUND,
                f"no commit with request_id {request_id!r}",
                details={"request_id": request_id},
            )
        return CommitLogView(
            request_id=record.request_id,
            table=record.table_name,
            operation=record.operation,
            status=record.status,
            base_snapshot_id=record.base_snapshot_id,
            final_snapshot_id=record.final_snapshot_id,
            rebased=record.rebased,
            attempts=record.attempts,
            error_category=record.error_category,
            error_message=record.error_message,
        ).model_dump()

    # -------------------------------------------------------------- orphans

    @app.get("/maintenance/orphans")
    async def scan_orphans(table: str | None = None) -> dict:
        return get_container().service.scan_orphans(table)

    @app.post("/maintenance/orphans/reconcile")
    async def reconcile_orphans(table: str | None = None) -> dict:
        return get_container().service.reconcile_orphans(table)

    @app.get("/maintenance/cleanup-records",
             response_model=list[CleanupRecordView])
    async def cleanup_records(status: str | None = None) -> list[dict]:
        c = get_container()
        return [
            CleanupRecordView(
                record_id=r.record_id,
                request_id=r.request_id,
                table_name=r.table_name,
                kind=r.kind,
                path=r.path if c.settings.log_full_paths else Path(r.path).name,
                status=r.status,
                error=r.error,
            ).model_dump()
            for r in c.ledger.list_records(status)
        ]

    @app.get("/diag/echo-request-id")
    async def echo_request_id() -> dict:
        """Show the id format used on every error/diagnostic record."""
        return {"request_id": new_request_id()}

    return app


_HTTP_STATUS: dict[ErrorCategory, int] = {
    ErrorCategory.VALIDATION: 400,
    ErrorCategory.CONFLICT_STALE_SNAPSHOT: 409,
    ErrorCategory.CONFLICT_OVERLAPPING_PARTITION: 409,
    ErrorCategory.CONFLICT_RETRY_EXHAUSTED: 409,
    ErrorCategory.STAGING_FAILED: 422,
    ErrorCategory.NOT_FOUND: 404,
    ErrorCategory.INDETERMINATE: 500,
}


app = create_app()

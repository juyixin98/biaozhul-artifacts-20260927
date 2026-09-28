"""FastAPI 验证接口。

仅做请求/响应模型与错误映射，不承载业务规则；规则在 kernel/service。
所有响应附带 X-Request-ID；DomainError 统一映射为带 reason_code 的 JSON。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from lake_txn import errors
from lake_txn.config import Settings, load_settings
from lake_txn.diagnostics import DiagnosticLogger, set_http_request_id
from lake_txn.service import LakeService, StageFileInput


class ColumnModel(BaseModel):
    name: str = Field(min_length=1)
    type: str = Field(min_length=1)


class CreateTableModel(BaseModel):
    table: str = Field(min_length=1)
    columns: list[ColumnModel]
    partition_column: str


class StageFileModel(BaseModel):
    logical_name: str = Field(min_length=1)
    mode: str = Field(pattern="^(inline|import)$")
    records: list[dict[str, Any]] | None = None
    source_path: str | None = None
    declared_partition: str | None = None
    declared_sha256: str | None = None


class StageModel(BaseModel):
    table: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    files: list[StageFileModel] = Field(min_length=1)


class CommitModel(BaseModel):
    table: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    kind: str = Field(pattern="^(APPEND|OVERWRITE)$")
    base_snapshot_id: int = Field(ge=0)
    files: list[str] = Field(min_length=1)  # logical_name 列表
    drop_partitions: list[str] | None = None


class SweepModel(BaseModel):
    grace_seconds: int | None = Field(default=None, ge=0)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    log = DiagnosticLogger(redact_fields=settings.redact_fields)
    service = LakeService(settings, log=log)
    app = FastAPI(
        title="简化湖表元数据事务服务",
        version="0.1.0",
        description="基于快照的乐观并发提交：分区不相交追加可合并，重叠覆盖必须冲突。"
        "这不是完整 Iceberg 实现。",
    )
    app.state.settings = settings
    app.state.service = service

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        rid = set_http_request_id(request.headers.get("x-request-id"))
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    @app.exception_handler(errors.DomainError)
    async def domain_error_handler(request: Request, exc: errors.DomainError):
        body = {
            "error": True,
            "reason_code": exc.reason_code,
            "message": exc.message,
            "detail": exc.detail,
            "request_id": getattr(request.state, "request_id", None),
        }
        if exc.status_code >= 500:
            log.error("request_failed", reason_code=exc.reason_code, path=request.url.path)
        else:
            log.warning(
                "request_rejected",
                reason_code=exc.reason_code,
                path=request.url.path,
                key_state=exc.detail,
            )
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "error": True,
                "reason_code": errors.VALIDATION_ERROR,
                "message": "请求模型校验失败",
                "detail": exc.errors(),
            },
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/tables", status_code=201)
    def create_table(body: CreateTableModel) -> dict[str, Any]:
        return service.create_table(
            body.table,
            [c.model_dump() for c in body.columns],
            body.partition_column,
        )

    @app.get("/v1/tables")
    def list_tables() -> dict[str, Any]:
        return service.list_tables()

    @app.post("/v1/staging/files", status_code=200)
    def stage_files(body: StageModel) -> dict[str, Any]:
        inputs = [
            StageFileInput(
                logical_name=f.logical_name,
                mode=f.mode,
                records=f.records,
                source_path=f.source_path,
                declared_partition=f.declared_partition,
                declared_sha256=f.declared_sha256,
            )
            for f in body.files
        ]
        return service.stage_files(body.table, body.request_id, inputs)

    @app.post("/v1/commits", status_code=200)
    def commit(body: CommitModel) -> dict[str, Any]:
        return service.commit(
            table=body.table,
            request_id=body.request_id,
            kind=body.kind,
            base_snapshot_id=body.base_snapshot_id,
            logical_names=body.files,
            drop_partitions=body.drop_partitions,
        )

    @app.get("/v1/commits/{request_id}")
    def commit_status(request_id: str, table: str | None = None) -> dict[str, Any]:
        return service.get_commit_status(request_id, table)

    @app.get("/v1/tables/{table}/snapshots")
    def list_snapshots(table: str) -> dict[str, Any]:
        return service.list_snapshots(table)

    @app.get("/v1/tables/{table}/snapshots/{snapshot_id}")
    def get_snapshot(table: str, snapshot_id: int) -> dict[str, Any]:
        return service.get_snapshot_detail(table, snapshot_id)

    @app.post("/v1/maintenance/sweep", status_code=200)
    def sweep(body: SweepModel) -> dict[str, Any]:
        return service.sweep(grace_seconds=body.grace_seconds)

    @app.get("/v1/maintenance/cleanup-ledger")
    def cleanup_ledger(request_id: str | None = None, kind: str | None = None) -> dict[str, Any]:
        return service.list_cleanup(request_id=request_id, kind=kind)

    return app

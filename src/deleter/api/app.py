"""FastAPI 应用：验证接口层。

每个变更/读接口都记录 run 日志（run_id、输入摘要、错误类别、内核 trace、
操作后状态、耗时）。run_id 在响应头 ``X-Run-Id`` 与响应体中返回，
可通过 GET /runs/{run_id} 完整重放。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..errors import DeleterError
from ..observability.run_logger import RunLogger, new_run_id
from ..service import DeleterService
from .schemas import (
    CreateTableRequest,
    DeleteBatchRequest,
    LoadRequest,
    QueryRequest,
    RewriteRequest,
)


def create_app(workspace: str | os.PathLike[str] | None = None) -> FastAPI:
    workspace = workspace or os.environ.get("DELETER_WORKSPACE", ".deleter_workspace")
    run_logger = RunLogger(Path(workspace))
    service = DeleterService(workspace, run_logger=run_logger)
    app = FastAPI(
        title="湖表读时删除应用器",
        version="1.0.0",
        description="支持文件行号删除与主键等值删除的纯后端服务",
    )
    app.state.service = service
    app.state.run_logger = run_logger

    # -------------- 错误翻译 --------------
    @app.exception_handler(DeleterError)
    async def _domain_error(_: Request, exc: DeleterError) -> JSONResponse:
        body = exc.to_dict()
        headers = {}
        rid = getattr(exc, "run_id", None)
        if rid:
            headers["X-Run-Id"] = rid
            body["error"]["details"] = {**body["error"].get("details", {}), "run_id": rid}
        return JSONResponse(status_code=exc.http_status, content=body, headers=headers)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # -------------- 表 --------------
    @app.post("/tables", status_code=201)
    async def create_table(req: CreateTableRequest, request: Request) -> dict[str, Any]:
        async def action():
            return service.create_table(req.table_id, req.columns, req.key_columns)
        return await _guarded(request, "/tables", action,
                              {"table_id": req.table_id,
                               "key_columns": req.key_columns,
                               "columns": req.columns}, req.table_id)

    @app.get("/tables")
    async def list_tables() -> dict[str, Any]:
        return {"tables": service.store.list_tables()}

    @app.get("/tables/{table_id}")
    async def describe_table(table_id: str, request: Request) -> dict[str, Any]:
        return await _guarded(request, "/tables/describe",
                              lambda: service.describe_table(table_id),
                              {"table_id": table_id}, table_id)

    @app.post("/tables/{table_id}/load", status_code=201)
    async def load(table_id: str, req: LoadRequest, request: Request) -> dict[str, Any]:
        async def action():
            return service.load_file(table_id, req.file_id, req.source)
        summary = {"table_id": table_id, "file_id": req.file_id,
                   "source_kind": req.source.get("kind"),
                   "inline_rows": len(req.source.get("rows", []))
                   if req.source.get("kind") == "inline" else None}
        return await _guarded(request, "/tables/load", action, summary, table_id)

    # -------------- 删除 --------------
    @app.post("/tables/{table_id}/deletes", status_code=200)
    async def deletes(table_id: str, req: DeleteBatchRequest, request: Request) -> dict[str, Any]:
        payload = [{"delete_id": d.delete_id, "kind": d.kind, "file_id": d.file_id,
                    "row_number": d.row_number, "key": d.key} for d in req.deletes]
        trace_events, collect = run_logger.trace_collector()
        async def action():
            res = service.apply_deletes(table_id, payload, trace=collect)
            res = dict(res)
            res["run_trace_tail"] = trace_events[-20:]
            return res
        out = await _guarded(request, "/tables/deletes", action,
                             {"table_id": table_id, "deletes": payload}, table_id,
                             trace=trace_events)
        return out

    # -------------- 重写 --------------
    @app.post("/tables/{table_id}/rewrite", status_code=200)
    async def rewrite(table_id: str, req: RewriteRequest, request: Request) -> dict[str, Any]:
        async def action():
            return service.rewrite_files(table_id, req.file_ids, req.new_file_id)
        return await _guarded(request, "/tables/rewrite", action,
                              {"table_id": table_id, "file_ids": req.file_ids,
                               "new_file_id": req.new_file_id}, table_id)

    # -------------- 读取 / 验证 --------------
    @app.post("/tables/{table_id}/query")
    async def query(table_id: str, req: QueryRequest, request: Request) -> dict[str, Any]:
        trace_events, collect = run_logger.trace_collector()

        async def action():
            return service.query(
                table_id,
                filters=[f.model_dump() for f in (req.filters or [])],
                columns=req.columns, trace=collect,
            )
        return await _guarded(request, "/tables/query", action,
                              {"table_id": table_id,
                               "filters": [f.model_dump() for f in (req.filters or [])],
                               "columns": req.columns}, table_id,
                              trace=trace_events)

    @app.get("/tables/{table_id}/snapshot")
    async def snapshot(table_id: str, request: Request) -> dict[str, Any]:
        return await _guarded(request, "/tables/snapshot",
                              lambda: service.snapshot(table_id),
                              {"table_id": table_id}, table_id)

    @app.get("/tables/{table_id}/rows/{file_id}/{row_number}")
    async def explain_row(table_id: str, file_id: str, row_number: int,
                          request: Request) -> dict[str, Any]:
        return await _guarded(
            request, "/tables/explain_row",
            lambda: service.explain_row(table_id, file_id, row_number),
            {"table_id": table_id, "file_id": file_id, "row_number": row_number}, table_id)

    @app.get("/tables/{table_id}/files")
    async def list_files(table_id: str) -> dict[str, Any]:
        service.store.get_table(table_id)
        return {"files": service.store.list_files(table_id)}

    @app.get("/tables/{table_id}/deletes")
    async def list_deletes(table_id: str) -> dict[str, Any]:
        service.store.get_table(table_id)
        return {"deletes": service.store.list_deletes(table_id)}

    # -------------- 运行日志 --------------
    @app.get("/runs")
    async def list_runs(limit: int = 100) -> dict[str, Any]:
        return {"runs": run_logger.list_runs(limit=limit)}

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        try:
            return run_logger.read_run(run_id)
        except FileNotFoundError:
            from ..errors import NotFoundError
            raise NotFoundError("运行编号不存在", run_id=run_id) from None

    return app


async def _guarded(
    request: Request,
    endpoint: str,
    action,
    request_summary: dict[str, Any],
    table_id: str | None,
    trace: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """统一执行+落 run 日志。成功与四类错误都可按 run_id 重放。"""
    service: DeleterService = request.app.state.service
    logger: RunLogger = request.app.state.run_logger
    run_id = new_run_id(endpoint.strip("/").split("/")[-1])
    t0 = time.perf_counter()
    try:
        result = action()
        if hasattr(result, "__await__"):
            result = await result
        duration_ms = (time.perf_counter() - t0) * 1000
        body = {"run_id": run_id, "result": result}
        logger.write_run(
            run_id, endpoint, request_summary,
            response_summary={"ok": True, "summary": _short(result)},
            kernel_trace=trace,
            state_after=service.state_summary(table_id) if table_id else {},
            duration_ms=round(duration_ms, 3), http_status=200,
        )
        return body
    except DeleterError as exc:
        duration_ms = (time.perf_counter() - t0) * 1000
        logger.write_run(
            run_id, endpoint, request_summary,
            response_summary=None, error_summary=exc.to_dict()["error"],
            kernel_trace=trace,
            state_after=service.state_summary(table_id) if table_id else {},
            duration_ms=round(duration_ms, 3), http_status=exc.http_status,
        )
        exc.run_id = run_id  # type: ignore[attr-defined]
        raise


def _short(result: Any) -> Any:
    """响应摘要：大对象只留计数，避免日志膨胀（完整结果在响应里）。"""
    if not isinstance(result, dict):
        return {"type": type(result).__name__}
    out: dict[str, Any] = {}
    for k, v in result.items():
        if k in ("rows",):
            out[k] = f"<{len(v)} rows>"
        elif k == "report" and isinstance(v, dict) and "verdicts" in v:
            out[k] = {kk: vv for kk, vv in v.items() if kk != "verdicts"}
        else:
            out[k] = v
    return out

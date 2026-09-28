"""FastAPI 应用：验证接口与统一错误信封、run_id 透传。

路由：
- POST /tables                        建表
- POST /tables/{id}/commits           提交快照（append/rewrite/position_delete/equality_delete）
- POST /tables/{id}/validate          只规划不落地（与 commit 同路径校验，返回规划阶段）
- GET  /tables/{id}/rows              读存活行（可按 snapshot_id/seq/filter/columns）
- POST /tables/{id}/explain           逐行处置解释（保留/删除/被过滤 + 判定理由）
- GET  /tables/{id}/snapshots         逐版本
- GET  /tables/{id}/events            逐版本领域事件
- GET  /tables/{id}/files             当前文件清单
- GET  /runs/{run_id}                 取运行日志（可重放：中间状态、理由、错误分类）
- GET  /healthz
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import Config
from app.contracts.models import (
    CommitModel,
    CreateTableModel,
    ExplainModel,
    FilterModel,
    ValidateModel,
)
from app.errors import AppError, http_status_for
from app.kernel import scanner
from app.metadata.store import Store
from app.services import committer, tables as table_service
from app.services.runner import RunRecorder


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.from_env()
    app = FastAPI(title="RTDA — 湖表读时删除应用器", version="1.0.0")
    app.state.config = config
    app.state.store = Store(config)

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        run_id = getattr(request.state, "run_id", None)
        return JSONResponse(status_code=exc.http_status, content=exc.envelope(run_id))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # ---- 建表 -----------------------------------------------------------
    @app.post("/tables")
    async def create_table(request: Request, body: CreateTableModel) -> dict[str, Any]:
        payload = await request.json()
        store: Store = request.app.state.store
        with RunRecorder(store).record(
            kind="CREATE", table_id=None, request=payload
        ) as (run_id, rec):
            request.state.run_id = run_id
            columns = [c.model_dump() for c in body.columns]
            rec.add("schema_validated", {"columns": len(columns), "primary_key": body.primary_key})
            result = table_service.create_table(
                store, run_id=run_id, name=body.name, columns=columns,
                primary_key=body.primary_key, config=body.config,
            )
            rec.add("table_created", {"table_id": result["table_id"]})
            return {"run_id": run_id, **result}

    # ---- 提交 -----------------------------------------------------------
    @app.post("/tables/{table_id}/commits")
    async def create_commit(table_id: str, request: Request, body: CommitModel) -> dict[str, Any]:
        payload = await request.json()
        store: Store = request.app.state.store
        with RunRecorder(store).record(
            kind="COMMIT", table_id=table_id, request=payload
        ) as (run_id, rec):
            request.state.run_id = run_id
            if body.table_id != table_id:
                from app.errors import ValidationError

                raise ValidationError(
                    "TABLE_ID_MISMATCH", "table_id in path and body differ",
                    {"path": table_id, "body": body.table_id},
                )
            result = committer.commit(
                store, run_id=run_id, table_id=table_id,
                parent_snapshot_id=body.parent_snapshot_id, operations=body.operations,
            )
            for phase in result.get("phases", []):
                rec.add(phase["phase"], phase.get("detail", {}))
            return {"run_id": run_id, **{k: v for k, v in result.items() if k != "phases"},
                    "phases": result.get("phases", [])}

    # ---- 只校验 ---------------------------------------------------------
    @app.post("/tables/{table_id}/validate")
    async def validate_commit(table_id: str, request: Request, body: ValidateModel) -> dict[str, Any]:
        payload = await request.json()
        store: Store = request.app.state.store
        with RunRecorder(store).record(
            kind="VALIDATE", table_id=table_id, request=payload
        ) as (run_id, rec):
            request.state.run_id = run_id
            if body.table_id != table_id:
                from app.errors import ValidationError

                raise ValidationError(
                    "TABLE_ID_MISMATCH", "table_id in path and body differ",
                    {"path": table_id, "body": body.table_id},
                )
            # 与 commit 完全相同的规划路径；不落盘、不提交
            from app.services.planner import plan_commit

            plan = plan_commit(
                store, table_id=table_id, parent_snapshot_id=body.parent_snapshot_id,
                operations=body.operations,
            )
            rec.add("plan_ok", {
                "appends": [{"ref": a.ref, "kind": a.kind, "rows": len(a.rows), "drops": a.drops}
                            for a in plan.appends],
                "position_deletes": [{"target_file": p.target_file_id, "positions": p.positions}
                                     for p in plan.position_deletes],
                "equality_deletes": [{"predicates": len(e.predicates)} for e in plan.equality_deletes],
            })
            return {"run_id": run_id, "valid": True, "phases": plan.phases}

    # ---- 读 -------------------------------------------------------------
    def _scan_kwargs(table_id: str, body: FilterModel | ExplainModel, explain: bool) -> dict[str, Any]:
        kw: dict[str, Any] = {
            "table_id": table_id,
            "snapshot_id": body.snapshot_id,
            "columns": body.columns,
            "filter": body.filter,
        }
        if getattr(body, "snapshot_seq", None) is not None:
            kw["seq"] = body.snapshot_seq
        if explain:
            kw["include_filtered"] = body.include_filtered
            kw["include_deleted"] = body.include_deleted
        else:
            kw["include_filtered"] = False
            kw["include_deleted"] = False
        return kw

    @app.get("/tables/{table_id}/rows")
    async def read_rows(
        table_id: str, request: Request,
        snapshot_id: str | None = None, seq: int | None = None,
        columns: str | None = None, filter: str | None = None,
    ) -> dict[str, Any]:
        store: Store = request.app.state.store
        params: dict[str, Any] = {
            "snapshot_id": snapshot_id, "seq": seq,
            "columns": columns.split(",") if columns else None,
            "filter": None,
        }
        from app.errors import ValidationError

        if filter is not None:
            try:
                parsed = json.loads(filter)
            except json.JSONDecodeError as exc:
                raise ValidationError(
                    "INVALID_FILTER", f"filter query parameter is not valid JSON: {exc}"
                )
            if not isinstance(parsed, dict):
                raise ValidationError("INVALID_FILTER", "filter must be a JSON object")
            params["filter"] = parsed
        with RunRecorder(store).record(kind="READ", table_id=table_id, request=params) as (run_id, rec):
            request.state.run_id = run_id
            result = scanner.run_scan(
                store, table_id=table_id, snapshot_id=snapshot_id, seq=seq,
                columns=params["columns"], filter=params["filter"],
                include_filtered=False, include_deleted=False,
            )
            rec.add("scan_complete", result.intermediate,
                    reason=f"{len(result.rows)} kept rows across {len(result.data_files)} files")
            return {
                "run_id": run_id,
                "table_id": table_id,
                "snapshot_id": result.snapshot_id,
                "seq": result.seq,
                "rows": [r["row"] for r in result.rows],
                "row_drivers": [{"file_id": r["file_id"], "position": r["position"]}
                                for r in result.rows],
            }

    @app.post("/tables/{table_id}/explain")
    async def explain(table_id: str, request: Request, body: ExplainModel) -> dict[str, Any]:
        payload = await request.json()
        store: Store = request.app.state.store
        with RunRecorder(store).record(kind="EXPLAIN", table_id=table_id, request=payload) as (run_id, rec):
            request.state.run_id = run_id
            result = scanner.run_scan(store, **_scan_kwargs(table_id, body, explain=True))
            rec.add("scan_complete", result.intermediate,
                    reason="disposition computed per row before filtering/projection")
            return {
                "run_id": run_id,
                "table_id": table_id,
                "snapshot_id": result.snapshot_id,
                "seq": result.seq,
                "intermediate_state": result.intermediate,
                "data_files": result.data_files,
                "null_keys_ignored": result.null_keys_ignored,
                "delete_files": result.delete_files,
                "rows": [
                    {
                        "file_id": r["file_id"],
                        "position": r["position"],
                        "added_seq": r["added_seq"],
                        "disposition": r["disposition"],
                        "reasons": r["reasons"],
                        "row": r["row"],
                    }
                    for r in result.rows
                ],
            }

    # ---- 元数据观察口 ---------------------------------------------------
    @app.get("/tables/{table_id}/snapshots")
    async def snapshots(table_id: str, request: Request) -> dict[str, Any]:
        store: Store = request.app.state.store
        rows = store.list_snapshots(table_id)
        return {"snapshots": [
            {"snapshot_id": r["snapshot_id"], "seq": r["seq"], "parent_id": r["parent_id"],
             "created_at": r["created_at"], "summary": json.loads(r["summary_json"])}
            for r in rows
        ]}

    @app.get("/tables/{table_id}/events")
    async def events(table_id: str, request: Request, seq: int | None = None) -> dict[str, Any]:
        store: Store = request.app.state.store
        rows = store.list_events(table_id, seq)
        return {"events": [
            {"id": r["id"], "run_id": r["run_id"], "ts": r["ts"], "snapshot_id": r["snapshot_id"],
             "seq": r["seq"], "event_type": r["event_type"], "payload": json.loads(r["payload_json"])}
            for r in rows
        ]}

    @app.get("/tables/{table_id}/files")
    async def files(table_id: str, request: Request) -> dict[str, Any]:
        store: Store = request.app.state.store
        table = store.get_table(table_id)
        if table is None:
            from app.errors import NotFound

            raise NotFound("TABLE_NOT_FOUND", f"table '{table_id}' not found")
        with store.read_conn() as conn:
            cur = store.current_snapshot(conn, table_id)
            if cur is None:
                return {"table_id": table_id, "current_seq": None, "live_files": [], "manifests": []}
            live = store.live_files_at(conn, table_id, cur["seq"])
            manifests = store.all_manifest_entries(conn, table_id)
        return {
            "table_id": table_id,
            "current_snapshot_id": cur["snapshot_id"],
            "current_seq": cur["seq"],
            "live_files": [
                {"file_id": r["file_id"], "path": r["path"], "content_hash": r["content_hash"],
                 "row_count": r["row_count"], "added_seq": r["added_seq"]}
                for r in live
            ],
            "manifests": [
                {"file_id": r["file_id"], "seq": r["seq"], "change": r["change"], "reason": r["reason"],
                 "snapshot_id": r["snapshot_id"]}
                for r in manifests
            ],
        }

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str, request: Request) -> dict[str, Any]:
        store: Store = request.app.state.store
        row = store.get_request_log(run_id)
        if row is None:
            from app.errors import NotFound

            raise NotFound("RUN_NOT_FOUND", f"run '{run_id}' not found")
        return {
            "run_id": row["run_id"], "ts": row["ts"], "kind": row["kind"],
            "table_id": row["table_id"], "status": row["status"],
            "request": json.loads(row["request_json"]),
            "phases": json.loads(row["phases_json"]),
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
            "duration_ms": row["duration_ms"],
        }

    return app


def app_factory() -> FastAPI:
    """uvicorn 入口: app.api.app:app_factory"""
    return create_app()

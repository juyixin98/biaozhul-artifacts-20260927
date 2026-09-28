"""FastAPI 应用工厂与路由。

错误一律返回结构化信封 {"error": {"code", "message"}}，未知异常记为
internal_error 且不伪装成功（http 500）。
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..config import AppConfig, load_config
from ..errors import Merge3Error
from ..service.merge_service import MergeService
from . import schemas


def create_app(config: AppConfig | None = None) -> FastAPI:
    cfg = config or load_config()
    app = FastAPI(
        title="immutable-snapshot 3-way merge backend",
        version=__version__,
        description="不可变表快照的开发分支与主分支三方合并后端",
    )
    app.state.config = cfg
    app.state.service = MergeService(cfg.storage)

    @app.exception_handler(Merge3Error)
    async def _domain_error(_: Request, exc: Merge3Error) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": str(exc)}},
        )

    @app.exception_handler(Exception)
    async def _unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        # 未知状态不统一返回成功：保留 500 与 internal_error
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": repr(exc)}},
        )

    service = app.state.service

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__}

    # ------------------------------------------------------------ 表

    @app.post("/api/v1/tables", status_code=201)
    def create_table(body: schemas.CreateTableIn) -> dict[str, Any]:
        spec = service.register_table(
            body.name, body.primary_key,
            [f.model_dump() for f in body.fields],
        )
        return {"table": spec.to_dict()}

    @app.get("/api/v1/tables/{table}")
    def get_table(table: str) -> dict[str, Any]:
        return {"table": service.get_spec(table).to_dict()}

    # ------------------------------------------------------------ 快照/分支

    @app.post("/api/v1/tables/{table}/snapshots", status_code=201)
    def write_snapshot(table: str, body: schemas.CommitSnapshotIn,
                       parent_snapshot_id: str | None = None) -> dict[str, Any]:
        snap = service.write_snapshot(table, body.rows,
                                      parent_snapshot_id=parent_snapshot_id)
        return {"snapshot": _snapshot(snap)}

    @app.get("/api/v1/tables/{table}/snapshots/{snapshot_id}")
    def get_snapshot(table: str, snapshot_id: str) -> dict[str, Any]:
        snap = service.store.get_snapshot(snapshot_id)
        from ..errors import NotFoundError
        if snap is None or snap.table != table:
            raise NotFoundError(f"快照不存在: {snapshot_id}")
        return {"snapshot": _snapshot(snap)}

    @app.get("/api/v1/tables/{table}/snapshots/{snapshot_id}/rows")
    def get_snapshot_rows(table: str, snapshot_id: str) -> dict[str, Any]:
        spec, rows = service.read_snapshot_rows(snapshot_id)
        return {"snapshot_id": snapshot_id, "row_count": len(rows), "rows": rows}

    @app.post("/api/v1/tables/{table}/branches/{name}", status_code=201)
    def create_branch(table: str, name: str, body: schemas.CreateBranchIn) -> dict[str, Any]:
        branch = service.create_branch(table, name, body.head_snapshot_id)
        return {"branch": branch.__dict__}

    @app.get("/api/v1/tables/{table}/branches")
    def list_branches(table: str) -> dict[str, Any]:
        return {"branches": [b.__dict__ for b in service.store.list_branches(table)]}

    @app.post("/api/v1/tables/{table}/branches/{name}/commits", status_code=201)
    def commit_to_branch(table: str, name: str, body: schemas.CommitRowsIn) -> dict[str, Any]:
        snap = service.commit_rows(table, name, body.rows)
        return {"snapshot": _snapshot(snap)}

    # ------------------------------------------------------------ 合并

    @app.post("/api/v1/merges", status_code=201)
    def start_merge(body: schemas.StartMergeIn) -> dict[str, Any]:
        run = service.start_merge(
            body.table, ours_branch=body.ours_branch,
            theirs_branch=body.theirs_branch,
            base_snapshot_id=body.base_snapshot_id,
        )
        return _run_view(run)

    @app.get("/api/v1/merges/{run_id}")
    def get_merge(run_id: str) -> dict[str, Any]:
        return _run_view(service.get_run(run_id))

    @app.post("/api/v1/merges/{run_id}/resolve", status_code=200)
    def resolve_merge(run_id: str, body: schemas.ResolveConflictIn) -> dict[str, Any]:
        run = service.resolve_conflict(
            run_id, body.key, body.kind, body.custom_row,
            binding=body.bound_snapshots,
        )
        return _run_view(run)

    @app.post("/api/v1/merges/{run_id}/commit", status_code=201)
    def commit_merge(run_id: str, body: schemas.CommitMergeIn) -> dict[str, Any]:
        snap = service.commit_merge(run_id, message=body.message,
                                    target_branch=body.target_branch)
        run = service.get_run(run_id)
        view = _run_view(run)
        view["merged_snapshot"] = _snapshot(snap)
        return view

    @app.post("/api/v1/merges/{run_id}/abandon", status_code=200)
    def abandon_merge(run_id: str) -> dict[str, Any]:
        return _run_view(service.abandon_merge(run_id))

    @app.get("/api/v1/tables/{table}/lineage")
    def lineage(table: str) -> dict[str, Any]:
        return service.lineage_graph(table)

    return app


def _snapshot(snap: Any) -> dict[str, Any]:
    return {
        "snapshot_id": snap.snapshot_id,
        "table": snap.table,
        "schema_version": snap.schema_version,
        "content_hash": snap.content_hash,
        "row_count": snap.row_count,
        "parent_snapshot_id": snap.parent_snapshot_id,
        "created_by_run_id": snap.created_by_run_id,
        "created_at": snap.created_at,
    }


def _run_view(run: Any) -> dict[str, Any]:
    plan = run.plan
    return {
        "run_id": run.run_id,
        "table": run.table,
        "status": run.status,
        "ours_branch": run.ours_branch,
        "theirs_branch": run.theirs_branch,
        "base_snapshot_id": run.base_snapshot_id,
        "ours_snapshot_id": run.ours_snapshot_id,
        "theirs_snapshot_id": run.theirs_snapshot_id,
        "bound_snapshots": [
            run.base_snapshot_id, run.ours_snapshot_id, run.theirs_snapshot_id
        ],
        "created_at": run.created_at,
        "committed_snapshot_id": run.committed_snapshot_id,
        "counts": {
            "total": len(plan.entries),
            "conflicts": len(plan.conflicts),
            "unresolved": len(plan.unresolved),
        },
        "entries": {k: e.to_dict() for k, e in plan.entries.items()},
        "conflicts": [
            {"key": c.key, "classification": c.classification, "reason": c.reason,
             "base_row": c.base_row, "ours_row": c.ours_row, "theirs_row": c.theirs_row,
             "resolution": c.resolution}
            for c in plan.conflicts.values()
        ],
    }

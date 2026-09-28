"""验证接口层（FastAPI）。

* 每个请求注入 X-Request-ID（客户端可传入以关联自己的输入），日志全部带同一 run_id；
* MergeError 子类映射为各自的 HTTP 状态码与 error_code，未知异常返回 500
  （error_code=INTERNAL_ERROR）并记录堆栈，绝不伪装成成功；
* 计划/解决/提交三步显式分离：无冲突分区自动合并，冲突必须显式解决后才能提交。
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import AppConfig, load_config
from .errors import MergeError
from .logging_setup import set_run_id, setup_logging
from .service import MergeService
from .storage import MetadataStore


# ---- 请求 / 响应模型 -------------------------------------------------------

class RefModel(BaseModel):
    branch: str | None = None
    commit_id: str | None = None


class ColumnModel(BaseModel):
    name: str
    type: str


class SchemaModel(BaseModel):
    table: str
    columns: list[ColumnModel]
    primary_key: list[str]


class IngestRequest(BaseModel):
    model_config = {"protected_namespaces": ()}

    schema_: SchemaModel = Field(alias="schema")
    rows: list[dict[str, Any]] = Field(default_factory=list)


class InitMainRequest(BaseModel):
    snapshot_id: str
    message: str = "initialize main"
    author: str = "tester"


class CreateBranchRequest(BaseModel):
    name: str
    ref: RefModel


class CommitRequest(BaseModel):
    snapshot_id: str
    message: str = "commit"
    author: str = "tester"


class PlanRequest(BaseModel):
    dev: RefModel
    main: RefModel | None = None          # 默认目标分支 main 的当前头
    target_branch: str = "main"


class ResolutionItem(BaseModel):
    row_key: str                          # 内核 key_string 形式，如 [3]
    action: str                           # USE_DEV / USE_MAIN / KEEP_DELETED / FIELD_PICK
    field_picks: dict[str, str] | None = None


class ResolveRequest(BaseModel):
    plan_id: str
    dev: RefModel
    target_branch: str = "main"
    resolutions: list[ResolutionItem]


class MergeCommitRequest(BaseModel):
    plan_id: str
    dev: RefModel
    target_branch: str = "main"
    message: str = "merge dev into main"
    author: str = "tester"
    # 也允许在提交同一请求里附带解决动作
    resolutions: list[ResolutionItem] | None = None


# ---- 应用装配 --------------------------------------------------------------

def create_app(config: AppConfig | None = None) -> FastAPI:
    cfg = config or load_config()
    setup_logging(cfg.log_level, cfg.log_file)
    store = MetadataStore(cfg.db_path, cfg.snapshot_dir)
    service = MergeService(store)

    app = FastAPI(
        title="Immutable Table Snapshot Three-Way Merge Backend",
        version=__version__,
        description="开发分支与主分支不可变表快照的三方合并后端（含血缘与冲突分类）",
    )
    app.state.service = service
    app.state.config = cfg

    @app.middleware("http")
    async def run_id_middleware(request: Request, call_next):
        run_id = request.headers.get("X-Request-ID") or f"run_{uuid.uuid4().hex[:12]}"
        set_run_id(run_id)
        request.state.run_id = run_id
        try:
            response = await call_next(request)
        except Exception as exc:  # 兜底：未知异常也要带 run_id 与明确失败码
            import traceback
            setup_logging().error(
                "event=http_unhandled_error path=%s error=%r\n%s",
                request.url.path, exc, traceback.format_exc(),
            )
            response = JSONResponse(
                status_code=500,
                content={"error_code": "INTERNAL_ERROR",
                         "message": f"unexpected error: {exc!r}",
                         "details": {}},
            )
        response.headers["X-Request-ID"] = run_id
        set_run_id("-")
        return response

    @app.exception_handler(MergeError)
    async def merge_error_handler(request: Request, exc: MergeError):
        setup_logging().warning("event=http_business_error code=%s msg=%s",
                                exc.error_code, exc.message)
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict(),
                            headers={"X-Request-ID": getattr(request.state, "run_id", "-")})

    # ---- 健康 / 元信息 -----------------------------------------------------

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": __version__,
                "branches": [b["name"] for b in store.list_branches()]}

    # ---- 快照摄取 ----------------------------------------------------------

    @app.post("/api/v1/snapshots", status_code=201)
    async def ingest(req: IngestRequest):
        payload = {
            "schema": {
                "table": req.schema_.table,
                "columns": [c.model_dump() for c in req.schema_.columns],
                "primary_key": req.schema_.primary_key,
            },
            "rows": req.rows,
        }
        return service.ingest_snapshot(payload)

    @app.get("/api/v1/snapshots/{snapshot_id}")
    async def get_snapshot(snapshot_id: str):
        return service.snapshot_rows(snapshot_id)

    # ---- 分支 / 提交 -------------------------------------------------------

    @app.post("/api/v1/repository/init", status_code=201)
    async def init_main(req: InitMainRequest):
        return service.initialize_main(req.snapshot_id, req.message, req.author)

    @app.get("/api/v1/branches")
    async def list_branches():
        return {"branches": store.list_branches()}

    @app.post("/api/v1/branches", status_code=201)
    async def create_branch(req: CreateBranchRequest):
        return service.create_branch(
            req.name,
            {"branch": req.ref.branch, "commit_id": req.ref.commit_id},
        )

    @app.post("/api/v1/branches/{branch_name}/commits", status_code=201)
    async def commit(branch_name: str, req: CommitRequest):
        return service.commit(branch_name, req.snapshot_id, req.message, req.author)

    # ---- 三方合并：计划 -> 解决 -> 提交 ------------------------------------

    @app.post("/api/v1/merges/plan")
    async def plan_merge(req: PlanRequest):
        main_ref = req.main.model_dump(exclude_none=True) if req.main else \
            {"branch": req.target_branch}
        plan = service.plan_merge(
            {"branch": req.dev.branch, "commit_id": req.dev.commit_id},
            main_ref, req.target_branch,
        )
        return service.plan_to_dict(plan)

    @app.post("/api/v1/merges/resolve")
    async def resolve(req: ResolveRequest):
        plan = service.rebuild_plan(
            {"branch": req.dev.branch, "commit_id": req.dev.commit_id},
            req.target_branch, req.plan_id,
        )
        items = [r.model_dump(exclude_none=True) for r in req.resolutions]
        return service.save_resolutions(plan, items)

    @app.post("/api/v1/merges/commit")
    async def merge_commit(req: MergeCommitRequest):
        plan = service.rebuild_plan(
            {"branch": req.dev.branch, "commit_id": req.dev.commit_id},
            req.target_branch, req.plan_id,
        )
        overrides = [r.model_dump(exclude_none=True) for r in req.resolutions] \
            if req.resolutions is not None else None
        return service.commit_merge(plan, req.message, req.author, overrides)

    # ---- 血缘 --------------------------------------------------------------

    @app.get("/api/v1/lineage")
    async def lineage(branch: str | None = None, commit_id: str | None = None):
        return service.lineage({"branch": branch, "commit_id": commit_id})

    return app


def create_app_from_path(config_path: str | None = None) -> FastAPI:
    return create_app(load_config(config_path))


app = create_app_from_path(None)

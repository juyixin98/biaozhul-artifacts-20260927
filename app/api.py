"""查询与诊断接口:FastAPI 应用。

端点:
- GET  /health
- POST /merge                              内联三方文本合并
- GET  /merges/{merge_id}                  查询合并记录(状态/冲突/说明)
- POST /merges/{merge_id}/resolve          按显式选择重建冲突
- POST /documents/{doc}/versions           存储一方版本快照
- GET  /documents/{doc}/versions           列出版本(不含内容)
- POST /documents/{doc}/merge              按版本号合并

每个请求带 request_id(可取 X-Request-ID,否则生成),所有诊断日志只写
脱敏指纹。失败响应统一为 {"error": {"category", "detail"}},类别见
ERROR_* 常量。
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import diagnostics
from .config import Settings, load_settings
from .merge import (
    Applied,
    UnknownChoiceError,
    UnresolvedConflictError,
    merge3,
    resolve3,
)
from .models import (
    ConflictModel,
    MergeByVersionsRequest,
    MergeRequest,
    MergeResponse,
    ResolveRequest,
    ResolveResponse,
    VersionRequest,
    VersionResponse,
)
from .store import Store

ERROR_MERGE_NOT_FOUND = "merge-not-found"
ERROR_VERSION_NOT_FOUND = "version-not-found"
ERROR_NOTHING_TO_RESOLVE = "nothing-to-resolve"
ERROR_UNRESOLVED_CONFLICTS = "unresolved-conflicts"
ERROR_UNKNOWN_CHOICE = "unknown-choice"
ERROR_INVALID_ROLE = "invalid-role"


def _error(status_code: int, category: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"category": category, "detail": detail}},
    )


def _conflict_models(outcome, base: str) -> list[ConflictModel]:
    from .textnorm import split_lines

    base_lines = split_lines(base)
    models = []
    for index, c in enumerate(outcome.conflicts):
        models.append(
            ConflictModel(
                index=index,
                kind=c.kind,
                base_range=[c.base_start, c.base_end],
                local_range=[c.local.start, c.local.end],
                remote_range=[c.remote.start, c.remote.end],
                local_lines=list(c.local.lines),
                base_lines=base_lines[c.base_start : c.base_end],
                remote_lines=list(c.remote.lines),
            )
        )
    return models


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    store = Store(settings.db_path)
    # 每个应用实例独立的 logger,避免测试间共享文件句柄
    logger = diagnostics.get_logger(settings.log_path, name=f"merge3.{uuid.uuid4().hex}")
    labels = (settings.local_label, settings.base_label, settings.remote_label)
    app = FastAPI(title="merge3", version="0.1.0")

    def new_request_id(request: Request) -> str:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        diagnostics.request_id_var.set(rid)
        return rid

    def run_merge(base: str, local: str, remote: str, document_id, rid: str) -> MergeResponse:
        outcome = merge3(base, local, remote, labels=labels)
        conflicts = _conflict_models(outcome, base)
        merge_id = store.save_merge(
            request_id=rid,
            document_id=document_id,
            base=base,
            local=local,
            remote=remote,
            status=outcome.status,
            result_text=outcome.text,
            conflicts=[c.model_dump() for c in conflicts],
            notes=outcome.notes,
        )
        diagnostics.log_event(
            logger,
            "merge.completed",
            merge_id=merge_id,
            document_id=document_id,
            status=outcome.status,
            base=base,
            local=local,
            remote=remote,
            applied={
                side: sum(
                    1
                    for d in outcome.decisions
                    if isinstance(d, Applied) and d.side == side
                )
                for side in ("local", "remote", "both")
            },
            conflict_kinds=[c.kind for c in outcome.conflicts],
            notes=outcome.notes,
        )
        return MergeResponse(
            merge_id=merge_id,
            request_id=rid,
            status=outcome.status,
            text=outcome.text,
            conflicts=conflicts,
            notes=outcome.notes,
        )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/merge", response_model=MergeResponse)
    def merge_inline(body: MergeRequest, request: Request) -> MergeResponse:
        rid = new_request_id(request)
        return run_merge(body.base, body.local, body.remote, body.document_id, rid)

    @app.get("/merges/{merge_id}")
    def get_merge(merge_id: str, request: Request):
        rid = new_request_id(request)
        record = store.get_merge(merge_id)
        if record is None:
            diagnostics.log_event(logger, "merge.lookup_failed", merge_id=merge_id,
                                  reason="merge-not-found")
            return _error(404, ERROR_MERGE_NOT_FOUND, f"no merge {merge_id}")
        import json

        diagnostics.log_event(logger, "merge.lookup", merge_id=merge_id,
                              status=record["status"])
        return {
            "merge_id": record["merge_id"],
            "request_id": rid,
            "document_id": record["document_id"],
            "status": record["status"],
            "text": record["result_text"],
            "conflicts": json.loads(record["conflicts_json"]),
            "notes": json.loads(record["notes_json"]),
            "resolved_text": record["resolved_text"],
            "resolutions": (
                json.loads(record["resolutions_json"])
                if record["resolutions_json"]
                else None
            ),
            "created_at": record["created_at"],
        }

    @app.post("/merges/{merge_id}/resolve", response_model=ResolveResponse)
    def resolve(merge_id: str, body: ResolveRequest, request: Request):
        rid = new_request_id(request)
        record = store.get_merge(merge_id)
        if record is None:
            diagnostics.log_event(logger, "resolve.rejected", merge_id=merge_id,
                                  reason="merge-not-found")
            return _error(404, ERROR_MERGE_NOT_FOUND, f"no merge {merge_id}")
        if record["status"] != "conflicted":
            diagnostics.log_event(logger, "resolve.rejected", merge_id=merge_id,
                                  reason="nothing-to-resolve")
            return _error(409, ERROR_NOTHING_TO_RESOLVE, "merge has no conflicts")
        try:
            choices = {int(k): v for k, v in body.choices.items()}
        except ValueError:
            return _error(422, ERROR_UNKNOWN_CHOICE, "choice keys must be conflict indices")
        try:
            resolved = resolve3(
                record["base"], record["local"], record["remote"], choices, labels=labels
            )
        except UnresolvedConflictError as exc:
            diagnostics.log_event(logger, "resolve.rejected", merge_id=merge_id,
                                  reason="unresolved-conflicts", missing=exc.missing)
            return _error(
                422, ERROR_UNRESOLVED_CONFLICTS,
                f"missing explicit choice for conflicts {exc.missing}",
            )
        except UnknownChoiceError as exc:
            diagnostics.log_event(logger, "resolve.rejected", merge_id=merge_id,
                                  reason="unknown-choice", choice=exc.choice)
            return _error(422, ERROR_UNKNOWN_CHOICE, str(exc))
        store.save_resolution(merge_id, resolved, choices)
        diagnostics.log_event(logger, "resolve.completed", merge_id=merge_id,
                              choices={str(k): v for k, v in choices.items()},
                              resolved_text=resolved)
        return ResolveResponse(merge_id=merge_id, request_id=rid, resolved_text=resolved)

    @app.post("/documents/{document_id}/versions", response_model=VersionResponse)
    def save_version(document_id: str, body: VersionRequest, request: Request):
        rid = new_request_id(request)
        try:
            saved = store.save_version(document_id, body.role, body.content)
        except ValueError as exc:
            diagnostics.log_event(logger, "version.rejected", document_id=document_id,
                                  reason="invalid-role")
            return _error(422, ERROR_INVALID_ROLE, str(exc))
        diagnostics.log_event(logger, "version.saved", document_id=document_id,
                              role=body.role, version_id=saved["version_id"],
                              content=body.content)
        return VersionResponse(**saved)

    @app.get("/documents/{document_id}/versions")
    def list_versions(document_id: str, request: Request):
        new_request_id(request)
        return {"document_id": document_id, "versions": store.list_versions(document_id)}

    @app.post("/documents/{document_id}/merge", response_model=MergeResponse)
    def merge_by_versions(document_id: str, body: MergeByVersionsRequest, request: Request):
        rid = new_request_id(request)
        texts = {}
        for role, version_id in (
            ("base", body.base_version),
            ("local", body.local_version),
            ("remote", body.remote_version),
        ):
            row = store.get_version(version_id)
            if row is None or row["document_id"] != document_id:
                diagnostics.log_event(logger, "merge.rejected", document_id=document_id,
                                      reason="version-not-found", version_id=version_id)
                return _error(404, ERROR_VERSION_NOT_FOUND,
                              f"no {role} version {version_id} in document {document_id}")
            texts[role] = row["content"]
        return run_merge(texts["base"], texts["local"], texts["remote"], document_id, rid)

    return app


app = create_app()

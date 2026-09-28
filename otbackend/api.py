"""HTTP 接口（FastAPI）。

路由
----
POST   /documents                       建文档
GET    /documents/{doc_id}              当前视图（rev/baseline/text）
POST   /documents/{doc_id}/submit       幂等提交操作
GET    /documents/{doc_id}/history      版本查询（?since=）
GET    /documents/{doc_id}/revisions/{rev}  单修订诊断
POST   /documents/{doc_id}/trim         历史裁剪（抬高基线）
GET    /documents/{doc_id}/catchup      旧客户端重建基线
GET    /healthz                         健康检查

错误响应统一为 errors.OTError.to_body 的 JSON 形状，带 request_id。
为了让所有输入错误（含 JSON 形状错误）都归入 INPUT_INVALID 类别，
请求体用 :meth:`Request.json` 自行解析，不走 pydantic 422。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import Settings
from .errors import InputInvalid, OTError
from .repository import SqliteRepository
from .service import OTService


def create_app(settings: Settings | None = None,
               service: OTService | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="Centralized Text OT (ins/del only)", version="1.0.0")
    app.state.settings = settings

    if service is None:
        repo = SqliteRepository(settings.db_path)
        service = OTService(
            repo,
            max_components=settings.max_components,
            max_doc_chars=settings.max_doc_chars,
            max_doc_bytes=settings.max_doc_bytes,
        )
    app.state.service = service

    def request_id(request: Request) -> str:
        return request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex[:16]}"

    @app.middleware("http")
    async def _attach_request_id(request: Request, call_next):
        # 统一为每个响应（含成功响应）附带 x-request-id，便于重放关联
        rid = request_id(request)
        request.state.rid = rid
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response

    @app.exception_handler(OTError)
    async def _ot_error(request: Request, exc: OTError):
        rid = request_id(request)
        return JSONResponse(
            status_code=exc.status,
            content=exc.to_body(rid),
            headers={"x-request-id": rid},
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):
        rid = request_id(request)
        body = {
            "error": {
                "code": "COMPUTATION_FAILED",
                "reason": "INTERNAL",
                "message": f"内部错误: {type(exc).__name__}: {exc}",
                "details": {},
                "request_id": rid,
            }
        }
        return JSONResponse(status_code=500, content=body,
                            headers={"x-request-id": rid})

    async def body_json(request: Request) -> dict[str, Any]:
        raw = await request.body()
        if not raw:
            raise InputInvalid("请求体为空", reason="EMPTY_BODY")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise InputInvalid(f"请求体不是合法 JSON: {e}", reason="BAD_JSON")
        if not isinstance(data, dict):
            raise InputInvalid("请求体必须是 JSON 对象", reason="BAD_BODY")
        return data

    def comp_dict(c) -> dict[str, Any]:
        return c.to_dict()

    # -------------------------------------------------- routes

    @app.post("/documents")
    async def create_document(request: Request):
        rid = request_id(request)
        data = await body_json(request)
        doc_id = data.get("doc_id")
        initial = data.get("initial_text", "")
        view = service.create_document(doc_id, initial)
        return {
            "request_id": rid,
            "doc_id": view.doc_id, "rev": view.rev,
            "baseline_rev": view.baseline_rev,
            "text": view.text, "length": view.length_chars,
        }

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "service": "text-ot", "version": "1.0.0"}

    @app.get("/documents/{doc_id}")
    async def get_document(doc_id: str, request: Request):
        rid = request_id(request)
        view = service.get_document(doc_id)
        return {
            "request_id": rid,
            "doc_id": view.doc_id, "rev": view.rev,
            "baseline_rev": view.baseline_rev,
            "text": view.text, "length": view.length_chars,
        }

    @app.post("/documents/{doc_id}/submit")
    async def submit(doc_id: str, request: Request):
        rid = request_id(request)
        data = await body_json(request)
        ack = service.submit(
            doc_id,
            base_rev=data.get("base_rev"),
            client_id=data.get("client_id"),
            client_op_id=data.get("client_op_id"),
            raw_ops=data.get("ops"),
        )
        return {
            "request_id": rid,
            "rev": ack.rev,
            "head_rev": ack.head_rev,
            "base_rev": ack.base_rev,
            "client_id": ack.client_id,
            "client_op_id": ack.client_op_id,
            "text": ack.text,
            "ops": [comp_dict(c) for c in ack.ops],
        }

    @app.get("/documents/{doc_id}/history")
    async def history(doc_id: str, request: Request, since: int = 0,
                      limit: int | None = None):
        rid = request_id(request)
        lim = limit or settings.history_page_limit
        revs = service.history(doc_id, since=since, limit=lim)
        return {
            "request_id": rid,
            "doc_id": doc_id,
            "baseline_rev": service.repo.baseline_rev(doc_id),
            "head_rev": service.repo.head_rev(doc_id),
            "revisions": [
                {
                    "rev": r.rev, "client_id": r.client_id,
                    "client_op_id": r.client_op_id, "base_rev": r.base_rev,
                    "length_before": r.length_before,
                    "length_after": r.length_after,
                    "checksum": r.checksum,
                    "ops": [comp_dict(c) for c in r.ops],
                }
                for r in revs
            ],
        }

    @app.get("/documents/{doc_id}/revisions/{rev}")
    async def revision_detail(doc_id: str, rev: int, request: Request):
        rid = request_id(request)
        r = service.revision_detail(doc_id, rev)
        return {
            "request_id": rid,
            "rev": r.rev, "client_id": r.client_id,
            "client_op_id": r.client_op_id, "base_rev": r.base_rev,
            "length_before": r.length_before, "length_after": r.length_after,
            "checksum": r.checksum, "ops": [comp_dict(c) for c in r.ops],
        }

    @app.post("/documents/{doc_id}/trim")
    async def trim(doc_id: str, request: Request):
        rid = request_id(request)
        data = await body_json(request)
        keep = data.get("keep_from_rev")
        if not isinstance(keep, int) or isinstance(keep, bool):
            raise InputInvalid("keep_from_rev 必须是整数", reason="BAD_TRIM_REV")
        out = service.snapshot_and_trim(doc_id, keep)
        out["request_id"] = rid
        return out

    @app.get("/documents/{doc_id}/catchup")
    async def catchup(doc_id: str, request: Request):
        rid = request_id(request)
        view = service.catchup(doc_id)
        return {
            "request_id": rid,
            "doc_id": view.doc_id, "rev": view.rev,
            "baseline_rev": view.baseline_rev,
            "text": view.text, "length": view.length_chars,
            "rebuild_required": True,
        }

    return app


def main():  # pragma: no cover - 进程入口
    import uvicorn

    settings = Settings.from_env()
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":  # pragma: no cover
    main()

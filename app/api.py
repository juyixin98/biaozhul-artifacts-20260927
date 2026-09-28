"""FastAPI HTTP 适配层。

错误响应统一为::

    {"error": {"code": ..., "category": ..., "message": ..., "details": {...}}}

``category`` 取值：input_error / state_conflict / resource_exhausted /
compute_failed，HTTP 状态码随错误而定（404/400/409/410/413/500）。
"""
from __future__ import annotations

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import OTError
from .models import Op
from .service import OTService
from .storage import Storage


class CreateDocBody(BaseModel):
    doc_id: str = Field(min_length=1)
    text: str = ""


class ComponentIn(BaseModel):
    # 与 app.models.Component.to_dict 对称的线上表示
    type: str
    n: int | None = None
    text: str | None = None
    client_id: str | None = None
    seq: int | None = None


class SubmitBody(BaseModel):
    client_id: str = Field(min_length=1)
    client_seq: int = Field(ge=1)
    base_revision: int = Field(ge=0)
    components: list[ComponentIn]

    def to_op(self) -> Op:
        return Op.from_dict({"components": [c.model_dump(exclude_none=True) for c in self.components]})


def create_app(service: OTService) -> FastAPI:
    app = FastAPI(
        title="Centralized insert/delete text-OT backend",
        version="1.0.0",
    )

    # ------------------------------------------------------- 错误处理
    @app.exception_handler(OTError)
    async def _ot_error_handler(request: Request, exc: OTError):
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.exception_handler(Exception)
    async def _unexpected_handler(request: Request, exc: Exception):
        # 不让非 OT 异常泄漏堆栈到线上；归为 compute_failed
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "unexpected_error",
                    "category": "compute_failed",
                    "message": str(exc)[:500],
                    "details": {"type": type(exc).__name__},
                }
            },
        )

    # ----------------------------------------------------------- 文档
    @app.post("/documents", status_code=201)
    def create_document(body: CreateDocBody):
        service.create_document(body.doc_id, body.text)
        return {"doc_id": body.doc_id, "head_revision": 0, "text": body.text}

    @app.get("/documents")
    def list_documents():
        return {"documents": service.list_documents()}

    @app.get("/documents/{doc_id}")
    def get_document(doc_id: str):
        return service.get_document(doc_id)

    # ----------------------------------------------------------- 操作
    @app.post("/documents/{doc_id}/ops")
    def submit_op(
        doc_id: str,
        body: SubmitBody,
        idempotency_key: str | None = Header(default=None),
    ):
        op = body.to_op()
        result = service.submit(
            doc_id=doc_id,
            client_id=body.client_id,
            client_seq=body.client_seq,
            base_revision=body.base_revision,
            op=op,
            idem_key=idempotency_key,
        )
        return {
            "revision": result.revision,
            "head_revision": result.head_revision,
            "base_revision": result.base_revision,
            "rebased": result.rebased,
            "replay": result.replay,
            "text": result.text,
            "op": result.op.to_dict(),
        }

    @app.get("/documents/{doc_id}/ops")
    def pull_ops(doc_id: str, after: int = 0, limit: int | None = None):
        pulled = service.pull(doc_id, after, limit)
        return {
            "doc_id": doc_id,
            "head_revision": pulled.head_revision,
            "pruned_horizon": pulled.horizon,
            "has_more": pulled.has_more,
            "text": pulled.text,
            "operations": [
                {
                    "revision": s.revision,
                    "client_id": s.client_id,
                    "client_seq": s.client_seq,
                    "base_revision": s.base_revision,
                    "created_at": s.created_at,
                    **s.op.to_dict(),
                }
                for s in pulled.ops
            ],
        }

    # ----------------------------------------------------------- 裁剪
    @app.post("/documents/{doc_id}/prune")
    def prune(doc_id: str, new_horizon: int):
        return service.prune(doc_id, new_horizon)

    # ----------------------------------------------------------- 诊断
    @app.post("/internal/faults/{doc_id}")
    def arm_fault(doc_id: str, times: int = 1):
        service.arm_fault(doc_id, times)
        return {"armed": times}

    @app.get("/diagnostics")
    def diagnostics():
        return service.diagnostics()

    return app


def build_default_app(db_path: str | None = None) -> FastAPI:
    """供 uvicorn ``app.api:app`` 直接加载。"""
    from .config import settings

    storage = Storage(db_path or settings.db_path)
    svc = OTService(storage, settings)
    return create_app(svc)


app = build_default_app()

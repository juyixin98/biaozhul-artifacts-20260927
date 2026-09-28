"""FastAPI 应用装配与请求标识中间件。"""

from __future__ import annotations

import threading
import uuid

from fastapi import FastAPI, Request

from .api.deps import AppState
from .api.errors_handler import register_exception_handlers
from .api.routes import router
from .config import Settings
from .diagnostics import Diagnostics, set_request_id
from .services.matcher import MatcherService
from .services.version_registry import VersionRegistry
from .storage.db import DiagnosticStore, connect, init_db

REQUEST_ID_HEADER = "X-Request-ID"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    app = FastAPI(
        title="Aho-Corasick 流式匹配后端",
        version="1.0.0",
        description=(
            "字节级 AC 自动机；支持模式集合版本、显式边界切换、跨块流式匹配、"
            "重叠/后缀命中与可恢复分页。"
        ),
    )

    conn = connect(settings.db_path)
    init_db(conn)

    registry = VersionRegistry(conn)
    diagnostics_store = DiagnosticStore(conn)
    diagnostics = Diagnostics(
        diagnostics_store, redact_payloads=settings.redact_payloads
    )
    service = MatcherService(
        conn,
        registry,
        diagnostics,
        cursor_secret=settings.cursor_secret,
        default_page_size=settings.default_page_size,
        max_page_size=settings.max_page_size,
    )

    app.state.deps = AppState(
        settings=settings,
        registry=registry,
        service=service,
        diagnostics=diagnostics,
        diagnostics_store=diagnostics_store,
        lock=threading.RLock(),
    )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        incoming = request.headers.get(REQUEST_ID_HEADER)
        # 透传调用方标识；缺省时生成。只接受受限字符集，避免日志/表头注入。
        if incoming and len(incoming) <= 128:
            request_id = incoming
        else:
            request_id = f"req_{uuid.uuid4().hex}"
        set_request_id(request_id)
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    register_exception_handlers(app)
    app.include_router(router)
    return app


app = create_app()

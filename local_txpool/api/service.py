"""服务组装入口：配置 -> 存储 -> 内核 -> FastAPI。"""

from __future__ import annotations

from fastapi import FastAPI

from ..core.clock import SystemClock
from ..core.config import Config, load_config
from ..core.kernel import Kernel
from ..storage.repository import Repository, connect, init_schema
from .app import create_app


def build_service(
    config: Config | None = None,
    *,
    db_path: str | None = None,
) -> tuple[FastAPI, Kernel, Config, Repository]:
    config = config or load_config()
    if db_path is not None:
        config.database_path = db_path
    conn = connect(config.database_path)
    init_schema(conn)
    repo = Repository(conn)
    kernel = Kernel(repo, config, SystemClock())
    app = create_app(kernel, config)
    return app, kernel, config, repo


def asgi_factory() -> FastAPI:
    """uvicorn/部署入口：只返回 ASGI 应用（内核挂在 app.state 上）。"""
    app, _kernel, _config, _repo = build_service()
    return app

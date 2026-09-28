"""FastAPI 应用工厂与启动装配。"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.api.state_service import StateService
from app.coding.params import TreeParams
from app.coding.signing import (
    Ed25519PrivateKey,
    generate_private_key,
    private_key_from_hex,
    private_key_from_pem,
    private_key_to_pem,
    public_key_to_pem,
)
from app.config import Settings, ensure_parent, get_settings
from app.core.smt import SparseMerkleTree
from app.diagnostics import Reason
from app.logging_setup import RequestAdapter, configure_logging
from app.storage.sqlite_store import SqliteStore


def _load_or_create_signer(settings: Settings) -> Ed25519PrivateKey:
    """签名密钥优先级：SMT_SIGNING_KEY_HEX > PEM 文件 > 自动生成开发密钥。"""
    if settings.signing_key_hex:
        return private_key_from_hex(settings.signing_key_hex)

    key_path = Path(settings.signing_key_path)
    if key_path.exists():
        return private_key_from_pem(key_path.read_bytes())

    # 本地合成环境：自动生成开发密钥（文件权限 0600），并写公钥旁车文件
    key = generate_private_key()
    ensure_parent(str(key_path))
    key_path.write_bytes(private_key_to_pem(key))
    key_path.chmod(0o600)
    key_path.with_suffix(key_path.suffix + ".pub").write_bytes(
        public_key_to_pem(key.public_key())
    )
    return key


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    ensure_parent(settings.db_path)

    logger = configure_logging()
    signer = _load_or_create_signer(settings)

    store = SqliteStore(settings.db_path)
    params = TreeParams(key_len=settings.key_len, depth=settings.depth)
    tree = SparseMerkleTree(store, params)
    service = StateService(store, tree, signer)

    app = FastAPI(
        title="固定键宽稀疏 Merkle 状态服务",
        version="0.1.0",
        description="更新、成员/非成员证明（压缩路径）、历史版本、离线回放",
    )
    app.state.settings = settings
    app.state.service = service
    app.state.logger = logger

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        incoming = request.headers.get("X-Request-ID")
        request.state.request_id = incoming or uuid.uuid4().hex
        try:
            response = await call_next(request)
        except Exception:
            # 未预期异常也要带请求标识，且不泄露内部细节
            log = RequestAdapter(logger, {"request_id": request.state.request_id})
            log.exception("unhandled error")
            return JSONResponse(
                status_code=500,
                content={
                    "error": "internal_error",
                    "reason": Reason.INCONCLUSIVE.value,
                    "message": "服务内部错误（详情见服务端日志，请求标识可用于追溯）",
                    "request_id": request.state.request_id,
                    "detail": {},
                },
                headers={"X-Request-ID": request.state.request_id},
            )
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    app.include_router(router)
    return app


app = create_app()

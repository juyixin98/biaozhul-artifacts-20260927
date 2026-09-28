"""HTTP 路由。

诊断约定：
* 每个响应（含错误）都带 ``X-Request-ID``；
* 拒绝/无法判定返回结构化错误体（error/reason/message/关键状态）；
* 日志不打印值，键仅打印脱敏指纹。
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.coding.keys import normalize_key, normalize_value
from app.diagnostics import Reason
from app.logging_setup import RequestAdapter, key_fingerprint, redact_keys
from app.offline.verifier import verify_envelope
from .proof_envelope import EnvelopeError, proof_from_envelope
from .schemas import (
    BatchUpdateRequest,
    ProofResponse,
    RootResponse,
    ValueResponse,
    VerifyRequest,
    VerifyResponse,
    VersionResponse,
)
from .state_service import IdempotencyConflict

router = APIRouter()


def _svc(request: Request):
    return request.app.state.service


def _logger(request: Request) -> RequestAdapter:
    return RequestAdapter(request.app.state.logger, {"request_id": request.state.request_id})


def _error(status: int, error: str, reason: Reason, message: str,
           request_id: str, detail: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": error,
            "reason": reason.value,
            "message": message,
            "request_id": request_id,
            "detail": detail or {},
        },
        headers={"X-Request-ID": request_id},
    )


@router.get("/health")
def health(request: Request) -> dict:
    return {"status": "ok", "version": _svc(request).current_version()}


@router.get("/root", response_model=RootResponse)
def root(request: Request, version: int | None = None) -> JSONResponse:
    svc = _svc(request)
    try:
        if version is None:
            cp = svc.store.latest_checkpoint()
        else:
            cp = svc.store.checkpoint(version)
            if cp is None:
                raise KeyError(version)
    except KeyError:
        return _error(404, "not_found", Reason.UNKNOWN_ROOT,
                      f"版本 {version} 不存在", request.state.request_id)
    body = RootResponse(
        version=cp["version"], root=cp["root"].hex(),
        parent_root=cp["parent_root"].hex() if cp["parent_root"] else None,
        batch_id=cp["batch_id"], changed=cp["changed"],
    )
    return JSONResponse(content=body.model_dump(), headers={"X-Request-ID": request.state.request_id})


@router.get("/value/{key_hex}", response_model=ValueResponse)
def get_value(request: Request, key_hex: str) -> JSONResponse:
    svc = _svc(request)
    log = _logger(request)
    try:
        key = normalize_key(key_hex, svc.params.key_len)
    except ValueError as exc:
        return _error(400, "bad_request", Reason.ENCODING_ERROR,
                      f"键编码错误: {exc}", request.state.request_id)
    value = svc.get_value(key)
    version = svc.current_version()
    log.info("lookup key_fp=%s exists=%s version=%s",
             key_fingerprint(key), value is not None, version)
    body = ValueResponse(
        key=key.hex(), exists=value is not None,
        value=None if value is None else value.hex(), version=version,
    )
    return JSONResponse(content=body.model_dump(), headers={"X-Request-ID": request.state.request_id})


@router.get("/proof/{key_hex}", response_model=ProofResponse)
def get_proof(request: Request, key_hex: str, version: int | None = None) -> JSONResponse:
    svc = _svc(request)
    log = _logger(request)
    try:
        key = normalize_key(key_hex, svc.params.key_len)
    except ValueError as exc:
        return _error(400, "bad_request", Reason.ENCODING_ERROR,
                      f"键编码错误: {exc}", request.state.request_id)
    try:
        envelope = svc.proof_for_key(key, version)
    except KeyError as exc:
        return _error(404, "not_found", Reason.UNKNOWN_ROOT,
                      str(exc), request.state.request_id)
    target_version = svc.current_version() if version is None else version
    value = svc.tree.get_value(bytes.fromhex(envelope["root"]), key)
    log.info("prove key_fp=%s end=%s version=%s",
             key_fingerprint(key), envelope["end"], target_version)
    body = ProofResponse(
        proof=envelope, exists=value is not None,
        value=None if value is None else value.hex(), version=target_version,
    )
    return JSONResponse(content=body.model_dump(), headers={"X-Request-ID": request.state.request_id})


@router.post("/updates", response_model=RootResponse)
def post_updates(request: Request, payload: BatchUpdateRequest) -> JSONResponse:
    svc = _svc(request)
    log = _logger(request)
    items: list[tuple[bytes, bytes | None]] = []
    try:
        for item in payload.updates:
            key = normalize_key(item.key, svc.params.key_len)
            value = normalize_value(item.value)
            items.append((key, value))
    except ValueError as exc:
        return _error(400, "bad_request", Reason.ENCODING_ERROR,
                      f"更新条目编码错误: {exc}", request.state.request_id)

    try:
        result = svc.apply_updates(items, idem_key=payload.idempotency_key)
    except IdempotencyConflict as exc:
        return _error(
            409, "idempotency_conflict", Reason.INCONCLUSIVE, str(exc),
            request.state.request_id,
            detail={"existing_version": exc.existing_version,
                    "existing_batch_id": exc.existing_batch_id},
        )
    log.info(
        "batch keys=[%s] version=%s changed=%s replay=%s",
        ",".join(redact_keys([k for k, _ in items])) or "-",
        result.version, result.changed, result.idempotent_replay,
    )
    body = RootResponse(
        version=result.version, root=result.root.hex(),
        parent_root=result.parent_root.hex(), batch_id=result.batch_id,
        changed=result.changed, idempotent_replay=result.idempotent_replay,
    )
    return JSONResponse(content=body.model_dump(), headers={"X-Request-ID": request.state.request_id})


@router.post("/verify", response_model=VerifyResponse)
def post_verify(request: Request, payload: VerifyRequest) -> JSONResponse:
    log = _logger(request)
    # 先用服务端解析器确保信封可解析（独立验证器内部也会解析，双重把关）
    try:
        proof, _params = proof_from_envelope(payload.proof)
    except EnvelopeError as exc:
        return _error(
            400, "verify_inconclusive", Reason.ENVELOPE_MALFORMED,
            f"证明信封无法解析: {exc}", request.state.request_id,
        )

    expect_value: bytes | None = None
    if payload.expect_value is not None:
        try:
            expect_value = normalize_value(payload.expect_value)
        except ValueError as exc:
            return _error(400, "verify_inconclusive", Reason.ENCODING_ERROR,
                          f"expect_value 编码错误: {exc}", request.state.request_id)

    # 调用独立离线验证器（不是内核核验函数）
    verdict = verify_envelope(
        payload.proof,
        expect_membership=payload.expect_membership,
        expect_value=expect_value,
    )
    log.info(
        "verify key_fp=%s decision=%s reason=%s root_known=%s",
        verdict.key_fingerprint or "-", verdict.decision.value, verdict.reason.value,
        svc_root_known(request, verdict.claimed_root),
    )
    status = 200 if verdict.accepted else 422
    body = VerifyResponse(**verdict.as_dict())
    return JSONResponse(
        status_code=status, content=body.model_dump(),
        headers={"X-Request-ID": request.state.request_id},
    )


def svc_root_known(request: Request, root_hex: str | None) -> bool:
    """诊断辅助：被验根是否属于本服务任一历史版本（不影响密码学结论）。"""
    if not root_hex:
        return False
    svc = _svc(request)
    target = bytes.fromhex(root_hex)
    for cp in svc.store.all_checkpoints():
        if cp["root"] == target:
            return True
    return False


@router.get("/versions/{version}", response_model=VersionResponse)
def get_version(request: Request, version: int) -> JSONResponse:
    cp = _svc(request).store.checkpoint(version)
    if cp is None:
        return _error(404, "not_found", Reason.UNKNOWN_ROOT,
                      f"版本 {version} 不存在", request.state.request_id)
    body = VersionResponse(
        version=cp["version"], root=cp["root"].hex(),
        parent_root=cp["parent_root"].hex() if cp["parent_root"] else None,
        batch_id=cp["batch_id"], changed=cp["changed"], created_at=cp["created_at"],
    )
    return JSONResponse(content=body.model_dump(), headers={"X-Request-ID": request.state.request_id})

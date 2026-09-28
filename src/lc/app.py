"""FastAPI service entry.

Endpoints
---------
POST /checkpoint           install out-of-band trusted checkpoint (once)
POST /headers/update       apply one signed header (optionally a rotation)
POST /replay               offline replay of an ordered captured bundle
GET  /head                 current trusted head + freshness
GET  /trust                trust-period status (needs_new_checkpoint?)
GET  /headers/{root}       inspect an indexed header
GET  /health               liveness + storage integrity

Errors are always ``{"error": {code, category, message, details, reasons}}``
with HTTP status mapped from the error category (see errors.HTTP_STATUS).
Request-shape validation failures are reported as category ``input_error``.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .chain import ChainKernel
from .clock import RunRecorder, SystemClock
from .config import KernelConfig
from .errors import Code, HTTP_STATUS, LightClientError
from .replay import parse_bundle
from .store import Store
from .types import Certificate, Checkpoint, Committee, Header


# --------------------------------------------------------------------------- #
# Request models (module scope so FastAPI can resolve their annotations).
# --------------------------------------------------------------------------- #
class MemberModel(BaseModel):
    public_key: str
    weight: int = Field(gt=0)


class CommitteeModel(BaseModel):
    members: List[MemberModel]


class HeaderModel(BaseModel):
    round: int = Field(ge=0)
    parent_root: str
    body_root: str
    timestamp: int = Field(ge=0)
    next_committee_commitment: Optional[str] = None


class SigModel(BaseModel):
    public_key: str
    signature: str


class CertificateModel(BaseModel):
    header_root: str
    signatures: List[SigModel]


class CheckpointReq(BaseModel):
    header: HeaderModel
    committee: CommitteeModel


class UpdateReq(BaseModel):
    header: Dict[str, Any]
    certificate: Dict[str, Any]
    next_committee: Optional[Dict[str, Any]] = None


class ReplayReq(BaseModel):
    items: List[Dict[str, Any]]


class AppState:
    def __init__(
        self,
        db_path: str = ":memory:",
        config: Optional[KernelConfig] = None,
        log_dir: Optional[str] = None,
    ):
        self.config = config or KernelConfig()
        self.store = Store(db_path)
        self.clock = SystemClock()
        self.recorder = RunRecorder(log_dir=log_dir)
        self.kernel = ChainKernel(
            self.store, self.clock, self.config, self.recorder
        )


def create_app(state: Optional[AppState] = None) -> FastAPI:
    state = state or AppState(
        db_path=os.environ.get("LC_DB_PATH", ":memory:"),
        log_dir=os.environ.get("LC_LOG_DIR") or None,
    )
    app = FastAPI(
        title="Local header light client (simplified protocol)",
        version="1.0.0",
        description="Educational fixed-local-chain light client. NOT compatible "
        "with any public blockchain.",
    )
    app.state.lc = state

    @app.exception_handler(LightClientError)
    async def _lc_error(_request: Request, exc: LightClientError) -> JSONResponse:
        return JSONResponse(
            status_code=HTTP_STATUS[exc.category],
            content={
                "error": exc.to_dict(),
                "run_id": state.recorder.run_id,
                "trace": exc.trace,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Shape/type errors before parsing -> INPUT category, HTTP 400.
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": Code.MALFORMED_HEADER.value,
                    "category": "input_error",
                    "message": "request does not match the required schema",
                    "details": {"validation": exc.errors()},
                    "reasons": [],
                },
                "run_id": state.recorder.run_id,
            },
        )

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        state.store.assert_consistent()
        return {"status": "ok", "run_id": state.recorder.run_id}

    @app.get("/head")
    async def head() -> Dict[str, Any]:
        return state.kernel.head()

    @app.get("/trust")
    async def trust() -> Dict[str, Any]:
        return state.kernel.trust_status()

    @app.post("/checkpoint", status_code=201)
    async def install_checkpoint(req: CheckpointReq) -> Dict[str, Any]:
        checkpoint = Checkpoint.from_dict(
            req.model_dump(), committee_max_size=state.config.committee_max_size
        )
        report = state.kernel.install_checkpoint(checkpoint)
        return {"result": report.to_dict()}

    @app.post("/headers/update")
    async def update_header(req: UpdateReq) -> Dict[str, Any]:
        header = Header.from_dict(req.header)
        certificate = Certificate.from_dict(req.certificate)
        next_committee = (
            None
            if req.next_committee is None
            else Committee.from_dict(
                req.next_committee, max_size=state.config.committee_max_size
            )
        )
        report = state.kernel.apply_header(header, certificate, next_committee)
        return {"result": report.to_dict()}

    @app.post("/replay")
    async def replay(req: ReplayReq) -> Dict[str, Any]:
        parsed = parse_bundle({"items": req.items}, state.config)
        report = state.kernel.apply_batch(parsed)
        return {"result": report.to_dict()}

    @app.get("/headers/{root_hex}")
    async def get_header(root_hex: str) -> JSONResponse:
        if not root_hex.startswith("0x"):
            root_hex = "0x" + root_hex
        try:
            root = bytes.fromhex(root_hex[2:])
        except ValueError:
            raise LightClientError(
                Code.MALFORMED_HEADER,
                "header root must be 0x-hex",
            )
        stored = state.store.get_header(root)
        if stored is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "HEADER_NOT_FOUND",
                        "category": "state_conflict",
                        "message": "no header indexed with that root",
                        "details": {"root": root_hex},
                        "reasons": [],
                    },
                    "run_id": state.recorder.run_id,
                },
            )
        return JSONResponse(
            content={"header": stored.header_json, "root": "0x" + stored.root.hex()}
        )

    return app


app = create_app()

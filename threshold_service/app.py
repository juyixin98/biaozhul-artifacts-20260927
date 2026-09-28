"""FastAPI 应用装配（薄适配层；安全决策全部在内核/策略层）。"""
from __future__ import annotations

import base64
import json
from functools import lru_cache

from fastapi import FastAPI, HTTPException, Query

from .audit import AuditLogger, new_request_id
from .config import Settings, load_settings
from .kernel import SecurityKernel
from .policy import (
    BAD_LENGTH,
    BAD_TAG,
    BELOW_THRESHOLD,
    FIELD_INCOMPATIBLE,
    MALFORMED_EVIDENCE,
    MIXED_SET,
    THRESHOLD_MISMATCH,
    UNKNOWN_SET,
)
from .repository import Repository
from .schemas import IssueRequest, IssueResponse, RecoverRequest, RecoverResponse
from .shamir import ShareError

_CATEGORY_STATUS = {
    MALFORMED_EVIDENCE: 422,
    MIXED_SET: 422,
    UNKNOWN_SET: 404,
    FIELD_INCOMPATIBLE: 422,
    THRESHOLD_MISMATCH: 422,
    BAD_TAG: 422,
    BAD_LENGTH: 422,
    BELOW_THRESHOLD: 403,
}


class _State:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.repo = Repository(settings.database_path, settings.master_key)
        self.audit = AuditLogger(settings.audit_path)
        self.kernel = SecurityKernel(settings, self.repo, self.audit)


@lru_cache(maxsize=1)
def _default_state() -> _State:
    return _State(load_settings())


def create_app(state: _State | None = None) -> FastAPI:
    state = state or _default_state()
    app = FastAPI(
        title="Threshold Secret Sharing Service",
        version="1.0.0",
        description="GF(2^8) Shamir 分片 + 独立 HMAC 完整性层（本地演示）",
    )
    app.state.tss = state

    @app.get("/health")
    def health():
        return {"status": "ok", "env": state.settings.env}

    @app.post("/sets", response_model=IssueResponse, status_code=201)
    def create_set(body: IssueRequest):
        request_id = new_request_id()
        try:
            secret = base64.b64decode(body.secret_b64, validate=True)
        except Exception:
            raise HTTPException(status_code=422, detail="secret_b64 must be valid base64")
        if not secret:
            raise HTTPException(status_code=422, detail="secret must be non-empty")
        try:
            result = state.kernel.issue_set(
                secret=secret, threshold=body.threshold,
                share_count=body.share_count, labels=body.labels,
                request_id=request_id,
            )
        except ShareError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return IssueResponse(**result.__dict__)

    @app.post("/recover", response_model=RecoverResponse)
    def recover(body: RecoverRequest):
        request_id = new_request_id()
        raw = [json.dumps(item, sort_keys=True, separators=(",", ":"))
               for item in body.shares]
        result = state.kernel.recover(raw, request_id=request_id)

        diagnostics = {
            "request_id": result.request_id,
            "submitted_count": result.evaluation.submitted_count,
            "distinct_x_count": result.evaluation.distinct_x_count,
            "threshold": result.evaluation.threshold,
            "set_id": result.evaluation.set_id,
            "field": result.evaluation.field,
            "malformed": result.evaluation.malformed,
            "bad_tag_fingerprints": result.evaluation.bad_tag_fingerprints,
            "field_mismatches": result.evaluation.field_mismatches,
            "threshold_mismatches": result.evaluation.threshold_mismatches,
            "bad_length_fingerprints": result.evaluation.bad_length_fingerprints,
            "duplicate_conflict_fingerprints":
                result.evaluation.duplicate_conflict_fingerprints,
            "repeated_fingerprints": result.evaluation.repeated_fingerprints,
            "accepted_fingerprints": result.evaluation.accepted_fingerprints,
        }
        response = RecoverResponse(
            outcome=result.outcome, category=result.category,
            request_id=result.request_id, set_id=result.set_id,
            reason=result.reason, secret_b64=result.secret_b64,
            secret_fp=result.secret_fp, diagnostics=diagnostics,
            consistent_subsets=result.consistent_subsets,
            always_good_x=result.always_good_x,
            enum_truncated=result.enum_truncated,
        )
        if result.outcome == "REJECTED":
            # 失败类别仍以 200 结构化返回（便于断言具体类别）；
            # 同时在 HTTP 语义上对硬拒绝给出状态码。
            status = _CATEGORY_STATUS.get(result.category, 422)
            raise HTTPException(
                status_code=status,
                detail={
                    "outcome": result.outcome,
                    "category": result.category,
                    "request_id": result.request_id,
                    "reason": result.reason,
                    "diagnostics": diagnostics,
                },
            )
        return response

    @app.get("/sets")
    def list_sets():
        rows = state.repo.list_sets()
        return [
            {
                "set_id": r["set_id"], "threshold": r["threshold"],
                "field": {"bits": r["field_bits"], "generator": r["field_gen"]},
                "secret_len": r["secret_len"], "secret_fp": r["secret_fp"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    @app.get("/sets/{set_id}")
    def get_set(set_id: str):
        row = state.repo.get_set(set_id)
        if row is None:
            raise HTTPException(status_code=404, detail="set not found")
        return {
            "set_id": row["set_id"], "threshold": row["threshold"],
            "field": {"bits": row["field_bits"], "generator": row["field_gen"]},
            "secret_len": row["secret_len"], "commitment": row["commitment"],
            "secret_fp": row["secret_fp"], "created_at": row["created_at"],
            "labels": json.loads(row["labels"]),
        }

    @app.get("/audit")
    def audit(
        request_id: str | None = Query(None),
        set_id: str | None = Query(None),
        limit: int = Query(100, ge=1, le=1000),
    ):
        return {"records": state.audit.query(request_id=request_id,
                                             set_id=set_id, limit=limit)}

    return app


def r_field(row):
    return {"bits": row["field_bits"], "generator": row["field_gen"]}


app = create_app()

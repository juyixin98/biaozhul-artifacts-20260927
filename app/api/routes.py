"""HTTP routes — thin transport layer over the services."""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.config import Settings
from app.domain.types import CanonicalEncodeError
from app.observability import get_logger
from app.services.batch_service import BatchService
from app.services.disclosure_service import DisclosureError, DisclosureService
from app.storage.db import Database
from app.verifier.independent import VerifyStatus, classify_item, verify_package
from app.version import (
    __version__,
    COMMITMENT_SCHEMA_VERSION,
    DISCLOSURE_SCHEMA_VERSION,
    MERKLE_SCHEMA_VERSION,
)

from .schemas import (
    AuditEntryOut,
    CreateBatchIn,
    CreateBatchOut,
    DiscloseIn,
    VerifyIn,
    VerifyOut,
)

router = APIRouter(prefix="/api/v1")
meta_router = APIRouter()


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def run_id(x_run_id: Annotated[str | None, Header()] = None) -> str:
    # Callers (tests, auditors) may correlate a run by supplying X-Run-ID;
    # otherwise one is minted and echoed back.
    return x_run_id or f"run-{uuid.uuid4().hex[:16]}"


RunId = Annotated[str, Depends(run_id)]
DbDep = Annotated[Database, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _http_for(code: str, message: str, run: str) -> HTTPException:
    status = {
        "BATCH_NOT_FOUND": 404,
        "UNKNOWN_FIELD": 404,
        "MALFORMED_REQUEST": 400,
        "ENCODE_ERROR": 422,
    }.get(code, 400)
    get_logger().warning(
        message, extra={"run_id": run, "verdict": code, "detail": {"http_status": status}}
    )
    return HTTPException(status_code=status, detail={"code": code, "message": message,
                                                      "run_id": run})


@meta_router.get("/health", tags=["meta"])
def health() -> dict:
    return {
        "status": "ok",
        "service_version": __version__,
        "commitment_schema_version": COMMITMENT_SCHEMA_VERSION,
        "merkle_schema_version": MERKLE_SCHEMA_VERSION,
        "disclosure_schema_version": DISCLOSURE_SCHEMA_VERSION,
    }


@router.post("/batches", response_model=CreateBatchOut, tags=["batches"])
def create_batch(body: CreateBatchIn, db: DbDep, settings: SettingsDep, run: RunId) -> CreateBatchOut:
    service = BatchService(db, settings, run)
    schema_raw = [s.model_dump() for s in body.schema_]
    try:
        created = service.create_batch(schema_raw, body.records)
    except CanonicalEncodeError as exc:
        db.append_audit(run_id=run, action="batch.create", verdict="ENCODE_ERROR",
                        detail={"error": str(exc)})
        raise _http_for("ENCODE_ERROR", str(exc), run) from exc
    return CreateBatchOut(
        batch_id=created.batch_id,
        root_hex=created.root_hex,
        record_count=created.record_count,
        schema_fields=created.schema,
        commitments=[
            {
                "leaf_index": c.leaf_index,
                "record_index": c.record_index,
                "field_position": c.field_position,
                "field_name": c.field_name,
                "field_type": c.field_type,
                "commitment_hex": c.commitment_hex,
            }
            for c in created.cells
        ],
        warnings=created.warnings,
    )


@router.get("/batches/{batch_id}", tags=["batches"])
def get_batch(batch_id: str, db: DbDep, settings: SettingsDep, run: RunId) -> dict:
    batch = db.get_batch(batch_id)
    if batch is None:
        raise _http_for("BATCH_NOT_FOUND", f"unknown batch: {batch_id}", run)
    service = BatchService(db, settings, run)
    return service.public_batch_view(batch, db.list_leaves(batch_id))


@router.post("/batches/{batch_id}/disclose", tags=["batches"])
def disclose(batch_id: str, body: DiscloseIn, db: DbDep, run: RunId) -> dict:
    service = DisclosureService(db, run)
    try:
        return service.issue(batch_id, [s.model_dump() for s in body.selectors])
    except DisclosureError as exc:
        raise _http_for(exc.code, str(exc), run) from exc


@router.post("/verify", response_model=VerifyOut, tags=["verify"])
def verify(body: VerifyIn, db: DbDep, run: RunId) -> VerifyOut:
    package = body.package
    log = get_logger()

    def progress(message: str) -> None:
        log.info("verify step: " + message, extra={"run_id": run,
                                                   "batch_id": package.get("batch_id")})

    report = verify_package(package, progress=progress)

    # Server-side, reference-assisted diagnosis for each item. The verdict
    # above is computed independently of the database; this mapping only
    # refines the failure category (e.g. WRONG_SALT vs WRONG_VALUE).
    diagnoses: dict[tuple[int, str], str] = {}
    batch_id = package.get("batch_id")
    if isinstance(batch_id, str):
        batch = db.get_batch(batch_id)
        if batch is not None:
            leaves = {(leaf.record_index, leaf.field_name): leaf
                      for leaf in db.list_leaves(batch_id)}
            for item in package.get("disclosed", []):
                key = (item.get("record_index"), item.get("field_name"))
                leaf = leaves.get(key) if isinstance(key[0], int) and isinstance(key[1], str) else None
                if leaf is not None:
                    diagnoses[(key[0], key[1])] = classify_item(item, {
                        "record_index": leaf.record_index,
                        "field_position": leaf.field_position,
                        "field_name": leaf.field_name,
                        "field_type": leaf.field_type,
                        "state": "present",
                        "commitment_hex": leaf.commitment_hex,
                        "salt_hex": leaf.salt_hex,
                    })

    out_items = []
    for it in report.items:
        detail = dict(it.detail)
        if (it.record_index, it.field_name) in diagnoses:
            detail["server_diagnosis"] = diagnoses[(it.record_index, it.field_name)]
        out_items.append({"record_index": it.record_index, "field_name": it.field_name,
                          "verdict": it.verdict, "detail": detail})

    db.append_audit(
        run_id=run, batch_id=batch_id if isinstance(batch_id, str) else None,
        action="disclosure.verify", verdict=report.verdict,
        detail={"valid": report.is_valid,
                "items": [[it.record_index, it.field_name, it.verdict] for it in report.items]},
    )
    return VerifyOut(
        verdict=report.verdict,
        valid=report.is_valid,
        root_hex=report.root_hex,
        recomputed_root_hex=report.recomputed_root_hex,
        items=out_items,
        errors=report.errors,
    )


@router.get("/audit", response_model=list[AuditEntryOut], tags=["audit"])
def audit_trail(db: DbDep, run: RunId, batch_id: str | None = None,
                limit: int = 100) -> list[AuditEntryOut]:
    limit = min(max(limit, 1), 1000)
    rows = db.list_audit(batch_id=batch_id, limit=limit)
    return [AuditEntryOut(seq=r.seq, ts=r.ts, run_id=r.run_id, batch_id=r.batch_id,
                          action=r.action, verdict=r.verdict, detail=r.detail) for r in rows]

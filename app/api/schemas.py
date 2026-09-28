"""Response schemas (explicit; never a fixed canned return)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ManifestFile(BaseModel):
    name: str
    physical: str
    size: int


class PlanSummary(BaseModel):
    container: str
    entries: int
    files: int
    directories: int
    symlinks: int
    total_declared_bytes: int
    compressed_bytes: int
    worst_compression_ratio: float
    max_depth_seen: int


class FailureResponse(BaseModel):
    run_id: str
    verdict: str  # "rejected" (policy/integrity) | "error" (unexpected)
    stage: str
    category: str
    message: str
    evidence: str | None = None
    input_sha256: str | None = None


class InspectResponse(BaseModel):
    run_id: str
    verdict: str  # "accepted"
    input_sha256: str
    filename: str
    plan: PlanSummary
    actions: list[str]


class ExtractResponse(BaseModel):
    run_id: str
    verdict: str  # "extracted"
    input_sha256: str
    filename: str
    output_dir: str
    plan: PlanSummary
    manifest: dict[str, Any]


class RunSummary(BaseModel):
    run_id: str
    created_at: str
    finalized_at: str | None = None
    verdict: str
    category: str | None = None
    container: str | None = None
    filename: str | None = None
    input_sha256: str | None = None
    input_size: int | None = None
    summary: dict | None = None


class EventOut(BaseModel):
    seq: int
    run_id: str
    ts: str
    version: str
    stage: str
    outcome: str
    category: str | None = None
    evidence: str | None = None
    message: str | None = None
    detail: dict | None = None
    prev_hash: str
    event_hash: str

"""Pydantic request/response models for the HTTP API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .algorithm import SegmentationResult, Segment


class SegmentRequest(BaseModel):
    text: str = Field(..., description="Raw text to segment (any length >= 1).")
    # None = use the newest published version; an integer pins one version
    # for this request and is honored even if a newer version appears later.
    version_id: int | None = Field(
        default=None, ge=1, description="Pinned lexicon version; omit for latest."
    )


class SegmentOut(BaseModel):
    surface: str
    type: Literal["dict", "unknown"]
    cost: float
    norm_start: int
    norm_end: int
    raw_start: int
    raw_end: int
    raw_text: str


class SegmentResponse(BaseModel):
    request_id: str
    version_id: int
    pinned: bool
    normalized_text: str
    deleted_raw_indices: list[int]
    char_map: list[int]
    segments: list[SegmentOut]
    best_cost: float
    second_best_cost: float | None
    cost_gap: float | None
    gap_class: Literal["clear", "close", "no_alternative"]
    decision: Literal["accepted", "indeterminate"]
    tie_broken: bool
    coverage: "CoverageOut"


class CoverageOut(BaseModel):
    raw_start: int
    raw_end: int
    raw_length: int
    contiguous: bool
    complete: bool


class ErrorResponse(BaseModel):
    request_id: str
    error: str  # machine-readable reason code
    message: str  # human-readable explanation


class WordIn(BaseModel):
    word: str = Field(..., min_length=1)
    freq: int = Field(..., ge=0)


class PublishRequest(BaseModel):
    words: list[WordIn] = Field(..., min_length=1)
    note: str = ""


class VersionOut(BaseModel):
    version_id: int
    published_at: str
    word_count: int
    total_freq: int
    checksum: str
    note: str


class PublishResponse(BaseModel):
    request_id: str
    version_id: int
    word_count: int
    total_freq: int
    checksum: str


def to_segment_out(s: Segment) -> SegmentOut:
    return SegmentOut(
        surface=s.surface,
        type=s.type,  # type: ignore[arg-type]
        cost=round(s.cost, 6),
        norm_start=s.norm_start,
        norm_end=s.norm_end,
        raw_start=s.raw_start,
        raw_end=s.raw_end,
        raw_text=s.raw_text,
    )


def check_coverage(segs: tuple[Segment, ...], raw_len: int) -> CoverageOut:
    """Verify segments cover raw_text exactly: start at 0, end at len, contiguous."""
    if not segs:
        return CoverageOut(raw_start=0, raw_end=0, raw_length=raw_len,
                           contiguous=True, complete=(raw_len == 0))
    contiguous = (
        segs[0].raw_start == 0
        and segs[-1].raw_end == raw_len
        and all(b.raw_start == a.raw_end for a, b in zip(segs, segs[1:]))
    )
    return CoverageOut(
        raw_start=0,
        raw_end=segs[-1].raw_end,
        raw_length=raw_len,
        contiguous=contiguous,
        complete=contiguous and segs[0].raw_start == 0 and segs[-1].raw_end == raw_len,
    )


def to_response(
    *,
    request_id: str,
    result: SegmentationResult,
    version_id: int,
    pinned: bool,
    raw_len: int,
) -> SegmentResponse:
    return SegmentResponse(
        request_id=request_id,
        version_id=version_id,
        pinned=pinned,
        normalized_text=result.normalized_text,
        deleted_raw_indices=list(result.deleted_raw_indices),
        char_map=list(result.char_map),
        segments=[to_segment_out(s) for s in result.segments],
        best_cost=round(result.best_cost, 6),
        second_best_cost=(
            None if result.second_best_cost is None else round(result.second_best_cost, 6)
        ),
        cost_gap=None if result.cost_gap is None else round(result.cost_gap, 6),
        gap_class=result.gap_class,  # type: ignore[arg-type]
        decision=result.decision,  # type: ignore[arg-type]
        tie_broken=result.tie_broken,
        coverage=check_coverage(result.segments, raw_len),
    )


SegmentResponse.model_rebuild()

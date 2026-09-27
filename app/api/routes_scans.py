"""Scan/session endpoints: open, feed, page, reset, close, status."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from ..diagnostics import Recorder, pattern_ref
from .deps import get_container, get_recorder
from .schemas import (
    FeedChunkIn,
    FeedChunkOut,
    HitPageOut,
    OpenScanIn,
    ResetIn,
    ScanStatusOut,
)

router = APIRouter(prefix="/scans", tags=["scans"])


@router.post("", response_model=ScanStatusOut, status_code=201)
def open_scan(
    body: OpenScanIn,
    container=Depends(get_container),
    rec: Recorder = Depends(get_recorder),
):
    scan_id, matcher = container.scans.open_scan(body.version_id)
    rec.record_decision(
        decision="accept",
        code="scan_opened",
        summary=(
            f"opened scan against version {body.version_id}: "
            f"{matcher.automaton.pattern_count} patterns, "
            f"{matcher.automaton.node_count} nodes"
        ),
        state={"scan_id": scan_id, "version_id": body.version_id,
               "node": 0, "bytes_consumed": 0, "epoch": 1},
    )
    return ScanStatusOut(**container.scans.status(scan_id))


@router.get("/{scan_id}", response_model=ScanStatusOut)
def scan_status(scan_id: str, container=Depends(get_container)):
    return ScanStatusOut(**container.scans.status(scan_id))


@router.post("/{scan_id}/chunks", response_model=FeedChunkOut)
def feed_chunk(
    scan_id: str,
    body: FeedChunkIn,
    container=Depends(get_container),
    rec: Recorder = Depends(get_recorder),
):
    result = container.scans.feed_chunk(
        scan_id,
        chunk_b64=body.chunk,
        declared_version_id=body.version_id,
    )
    # Log only a length; never the chunk bytes. If hits landed, log refs of
    # the *pattern ids* (still content-free) at most in a capped sample.
    import base64
    sample = []
    if result["new_hits"]:
        first_seq = result["first_seq"] or 0
        page = container.scan_repo.page_hits(
            scan_id, result["epoch"], first_seq - 1, 5
        )
        pids = [r["pat_id"] for r in page[:5]]
        lengths = container.scans.pattern_lengths(scan_id, pids)
        sample = [pattern_ref(pid, length)
                  for pid, length in zip(pids, lengths)]
    rec.record_decision(
        decision="accept",
        code="chunk_accepted",
        summary=(
            f"consumed chunk: {len(base64.b64decode(body.chunk))} input bytes; "
            f"{result['new_hits']} new hit(s); node={result['state_node']} "
            f"total={result['bytes_consumed']}"
        ),
        state={
            "scan_id": scan_id,
            "version_id": result["version_id"],
            "node": result["state_node"],
            "bytes_consumed": result["bytes_consumed"],
            "epoch": result["epoch"],
            "new_hits": result["new_hits"],
            "hit_sample_patterns": sample,
        },
    )
    return FeedChunkOut(**result)


@router.get("/{scan_id}/hits", response_model=HitPageOut)
def get_hits(
    scan_id: str,
    container=Depends(get_container),
    cursor: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=None),
):
    page = container.scans.page_hits(
        scan_id, cursor_token=cursor, limit=limit
    )
    return HitPageOut(**page)


@router.post("/{scan_id}/reset", response_model=ScanStatusOut)
def reset_scan(
    scan_id: str,
    body: ResetIn,
    container=Depends(get_container),
    rec: Recorder = Depends(get_recorder),
):
    status = container.scans.reset_scan(scan_id, body.version_id)
    rec.record_decision(
        decision="accept",
        code="scan_reset_at_boundary",
        summary=(
            f"explicit boundary: scan rebound to version {body.version_id}, "
            f"state rewound to root, epoch now {status['epoch']}"
        ),
        state=status,
    )
    return ScanStatusOut(**status)


@router.post("/{scan_id}/close", response_model=ScanStatusOut)
def close_scan(
    scan_id: str,
    container=Depends(get_container),
    rec: Recorder = Depends(get_recorder),
):
    status = container.scans.close_scan(scan_id)
    rec.record_decision(
        decision="accept",
        code="scan_closed",
        summary=f"scan {scan_id} closed",
        state=status,
    )
    return ScanStatusOut(**status)

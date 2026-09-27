"""Pattern-version endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..diagnostics import Recorder
from .deps import get_container, get_recorder
from .schemas import CreateVersionIn, VersionOut

router = APIRouter(prefix="/versions", tags=["versions"])


@router.post("", response_model=VersionOut, status_code=201)
def create_version(
    body: CreateVersionIn,
    container=Depends(get_container),
    rec: Recorder = Depends(get_recorder),
):
    version_id, spec, automaton = container.versions.create_version(
        body.patterns,
        encoding=body.encoding,
        case_mode=body.case_mode,
        name=body.name,
    )
    # Sensitive-free state: counts only, never pattern contents.
    rec.record_decision(
        decision="accept",
        code="version_created",
        summary=(
            f"accepted {automaton.pattern_count} patterns under "
            f"{spec.encoding}/{spec.case_mode.value}; "
            f"automaton has {automaton.node_count} nodes"
        ),
        state={
            "version_id": version_id,
            "pattern_count": automaton.pattern_count,
            "node_count": automaton.node_count,
            "spec": spec.to_dict(),
        },
    )
    row = container.versions.describe(version_id)
    return VersionOut(**row)


@router.get("", response_model=list[VersionOut])
def list_versions(
    container=Depends(get_container),
):
    return [VersionOut(**r) for r in container.versions.list_versions()]


@router.get("/{version_id}", response_model=VersionOut)
def get_version(
    version_id: str,
    container=Depends(get_container),
):
    return VersionOut(**container.versions.describe(version_id))

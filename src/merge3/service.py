"""Application service: orchestrates engine, storage and diagnostics.

Failure categories (the API maps these to HTTP status codes):

* ``input_invalid``       -> 400 — structurally bad input (wrong type/empty id)
* ``payload_too_large``   -> 413 — document exceeds MERGE3_MAX_DOCUMENT_CHARS
* ``not_found``           -> 404 — merge/document/version id is unknown
* ``resolution_invalid``  -> 409 — resolution set incomplete/illegal
* ``merge_indeterminate`` -> 200 body status=conflict — not an error status:
  the core did its job and is reporting that it cannot decide without an
  explicit choice.  The log event distinguishes this from acceptance.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Optional

from .config import Settings
from .diagnostics import DiagnosticLogger, document_state, new_request_id
from .merge import MergeEngine, MergeInputError, ResolutionError, three_way_merge
from .model import ConflictBlock, MergeResult
from .storage import VersionStore


class ServiceError(Exception):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


@dataclass
class MergeResponse:
    merge_id: str
    request_id: str
    status: str  # "auto" | "conflict"
    merged_text: Optional[str]
    conflicts: list[dict[str, Any]]
    diagnostics: list[dict[str, Any]]
    edit_summary: dict[str, int]


@dataclass
class RebuildResponse:
    merge_id: str
    request_id: str
    status: str
    merged_text: str


class MergeService:
    def __init__(self, store: VersionStore, settings: Settings,
                 logger: Optional[DiagnosticLogger] = None) -> None:
        self.store = store
        self.settings = settings
        self._engines: dict[str, MergeEngine] = {}
        self._results: dict[str, MergeResult] = {}

    # ------------------------------------------------------------------ #

    def _validate_text(self, name: str, value: Any) -> str:
        if not isinstance(value, str):
            raise ServiceError(
                "input_invalid",
                f"{name} must be a JSON string, got {type(value).__name__}",
            )
        if len(value) > self.settings.max_document_chars:
            raise ServiceError(
                "payload_too_large",
                f"{name} has {len(value)} chars; limit is "
                f"{self.settings.max_document_chars}",
            )
        return value

    # ------------------------------------------------------------------ #

    def run_merge(self, base_text: Any, local_text: Any, remote_text: Any,
                  document_id: str = "default",
                  request_id: Optional[str] = None) -> MergeResponse:
        request_id = request_id or new_request_id()
        diag = DiagnosticLogger(request_id=request_id, redact=self.settings.log_redact_secrets)

        base = self._validate_text("base_text", base_text)
        local = self._validate_text("local_text", local_text)
        remote = self._validate_text("remote_text", remote_text)
        if not document_id or not isinstance(document_id, str):
            raise ServiceError("input_invalid", "document_id must be a non-empty string")

        diag.event(
            "merge_started",
            state={
                "document_id": document_id,
                "base": document_state("base", base),
                "local": document_state("local", local),
                "remote": document_state("remote", remote),
            },
            reason="three-way merge requested",
        )

        try:
            engine, result = three_way_merge(base, local, remote, request_id)
        except MergeInputError as exc:
            diag.event("merge_rejected",
                       state={"documents": [document_id]},
                       reason=f"structural validation failed: {exc}",
                       level=40)  # logging.ERROR
            raise ServiceError("input_invalid", str(exc)) from exc

        merge_id = "mg_" + uuid.uuid4().hex[:16]
        self._engines[merge_id] = engine
        self._results[merge_id] = result

        # Persist inputs and outcome.
        self.store.ensure_document(document_id)
        base_v = self.store.add_version(document_id, "base", base)
        local_v = self.store.add_version(document_id, "local", local,
                                         parent_version_id=base_v)
        remote_v = self.store.add_version(document_id, "remote", remote,
                                          parent_version_id=base_v)

        if result.auto_merged:
            merged_v = self.store.add_version(document_id, "merged",
                                              result.merged_text or "",
                                              parent_version_id=base_v)
            self.store.record_merge(
                merge_id=merge_id, request_id=request_id, document_id=document_id,
                base_version_id=base_v, local_version_id=local_v,
                remote_version_id=remote_v, status="auto",
                merged_version_id=merged_v)
            diag.event(
                "merge_accepted",
                state={
                    "merge_id": merge_id,
                    "local_edit_count": len(result.local_edits),
                    "remote_edit_count": len(result.remote_edits),
                    "decisions": engine.decisions,
                    "merged": document_state("merged", result.merged_text or ""),
                },
                reason=("all edits disjoint or identical; no choice was "
                        "required for the core to produce this text"),
            )
            status = "auto"
        else:
            self.store.record_merge(
                merge_id=merge_id, request_id=request_id, document_id=document_id,
                base_version_id=base_v, local_version_id=local_v,
                remote_version_id=remote_v, status="conflict",
                conflicts=[b.to_dict() for b in result.conflicts])
            diag.event(
                "merge_indeterminate",
                state={
                    "merge_id": merge_id,
                    "conflict_count": len(result.conflicts),
                    "conflict_types": [b.conflict_type.value
                                       for b in result.conflicts],
                    "conflicts": [
                        {
                            "conflict_id": b.conflict_id,
                            "type": b.conflict_type.value,
                            "base_region": b.base_region.__dict__,
                            "local_region": b.local_region.__dict__,
                            "remote_region": b.remote_region.__dict__,
                            "local_edit_ids": list(b.local_edit_ids),
                            "remote_edit_ids": list(b.remote_edit_ids),
                            "base_chars": len(b.base_text),
                            "local_chars": len(b.local_text),
                            "remote_chars": len(b.remote_text),
                        }
                        for b in result.conflicts
                    ],
                    "decisions": engine.decisions,
                },
                reason=("two sides require different content in the same "
                        "region; the core will not guess, so an explicit "
                        "resolution per conflict is required"),
                level=30,  # logging.WARNING
            )
            status = "conflict"

        return MergeResponse(
            merge_id=merge_id,
            request_id=request_id,
            status=status,
            merged_text=result.merged_text,
            conflicts=[b.to_dict() for b in result.conflicts],
            diagnostics=diag.records,
            edit_summary={
                "local_edits": len(result.local_edits),
                "remote_edits": len(result.remote_edits),
                "conflicts": len(result.conflicts),
            },
        )

    # ------------------------------------------------------------------ #

    def resolve(self, merge_id: Any,
                resolutions: Any,
                document_id: str = "default") -> RebuildResponse:
        if not isinstance(merge_id, str) or not merge_id:
            raise ServiceError("input_invalid", "merge_id must be a non-empty string")
        engine = self._engines.get(merge_id)
        result = self._results.get(merge_id)
        if engine is None or result is None:
            # Fall back to storage so a resolved merge survives in-process
            # metadata loss for the persisted portion (engines are ephemeral).
            raise ServiceError("not_found", f"unknown merge_id {merge_id!r}")
        if not isinstance(resolutions, dict):
            raise ServiceError("input_invalid",
                               "resolutions must be an object {conflict_id: spec}")
        for cid, spec in resolutions.items():
            if not isinstance(spec, dict) or "choice" not in spec:
                raise ServiceError(
                    "input_invalid",
                    f"resolution for {cid!r} must be an object with 'choice'",
                )

        diag = DiagnosticLogger(request_id=result.request_id,
                                redact=self.settings.log_redact_secrets)
        try:
            merged = engine.rebuild(result, resolutions)
        except ResolutionError as exc:
            diag.event("resolution_rejected",
                       state={"merge_id": merge_id,
                              "submitted_ids": sorted(resolutions)},
                       reason=str(exc),
                       level=40)
            raise ServiceError("resolution_invalid", str(exc)) from exc

        self.store.ensure_document(document_id)
        merged_v = self.store.add_version(
            document_id, "merged", merged,
            version_id=f"merged_{merge_id}_{uuid.uuid4().hex[:8]}")
        self.store.attach_merged_version(merge_id, merged_v)
        for cid, spec in resolutions.items():
            self.store.record_resolution(
                merge_id, cid, spec["choice"],
                spec.get("text") if spec.get("choice") == "custom_text" else None)

        diag.event(
            "resolution_rebuilt",
            state={
                "merge_id": merge_id,
                "resolved": sorted(resolutions),
                "choices": {cid: spec["choice"]
                            for cid, spec in resolutions.items()},
                "merged": document_state("merged", merged),
            },
            reason="every conflict received an explicit valid choice",
        )
        return RebuildResponse(merge_id=merge_id, request_id=result.request_id,
                               status="rebuilt", merged_text=merged)

    # ------------------------------------------------------------------ #

    def get_merge(self, merge_id: str) -> dict[str, Any]:
        record = self.store.get_merge(merge_id)
        if record is None:
            raise ServiceError("not_found", f"unknown merge_id {merge_id!r}")
        record["conflicts"] = self.store.get_conflicts(merge_id)
        record["resolutions"] = self.store.get_resolutions(merge_id)
        result = self._results.get(merge_id)
        if result is not None and result.auto_merged:
            record["merged_text"] = result.merged_text
        elif record.get("merged_version_id"):
            version = self.store.get_version(record["merged_version_id"])
            record["merged_text"] = version["content"] if version else None
        else:
            record["merged_text"] = None
        return record

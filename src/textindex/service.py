"""Service layer: orchestrates encoding → normalization → index → storage.

This is the only layer the HTTP API and the replay CLI use, so both get
identical behaviour and identical diagnostics.  Every public method records
a structured log entry (success or categorized failure) with enough
intermediate state to replay it.
"""

from __future__ import annotations

import secrets
from typing import Any

from . import encoding, index as index_mod, normalizer
from .config import Settings
from .diagnostics import RunLogger
from .edits import Edit, EditResult, UNITS, apply_edit
from .errors import (
    DocumentTooLarge,
    EmptyDocument,
    InvalidUnit,
    TextIndexError,
)
from .storage import StoredDocument, Storage
from .unicode_version import DATA_VERSION_IDENTITY, UNICODE_VERSION


def new_doc_id() -> str:
    return "doc_" + secrets.token_hex(6)


class TextIndexService:
    def __init__(self, settings: Settings, *, storage: Storage | None = None,
                 logger: RunLogger | None = None) -> None:
        self.settings = settings
        self.storage = storage or Storage(settings.db_path)
        self.logger = logger or RunLogger(settings.log_path)
        self.run_id = self.logger.run_id

    def close(self) -> None:
        self.logger.close()
        self.storage.close()

    # --- create / get / delete ---------------------------------------------

    def create_document(
        self,
        raw: bytes | str,
        *,
        doc_id: str | None = None,
        normalization: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        form = (normalization or self.settings.default_normalization).upper()
        key = doc_id or "(auto)"
        try:
            text, decode_note = self._ingest(raw)
            if not text:
                raise EmptyDocument()
            size = len(text.encode("utf-8"))
            if size > self.settings.max_document_bytes:
                raise DocumentTooLarge(size, self.settings.max_document_bytes)
            canonical = normalizer.canonicalize(text, form)
            idx = index_mod.build_index(
                canonical, max_clusters=self.settings.max_clusters
            )
            chosen_id = doc_id or new_doc_id()
            stored = self.storage.create(chosen_id, canonical, form, idx)
            self.logger.record(
                op="documents/create", outcome="ok", key=stored.doc_id,
                request_id=request_id,
                intermediate={
                    "input_bytes": size,
                    "normalization": form,
                    "clusters": idx.cluster_count,
                    "codepoints": idx.codepoint_count,
                    "decode": decode_note,
                },
            )
            return self._doc_view(stored)
        except TextIndexError as exc:
            self.logger.record_error(
                op="documents/create", exc=exc, key=key, request_id=request_id,
                intermediate={"normalization": form},
            )
            raise

    def get_document(self, doc_id: str, *,
                     request_id: str | None = None) -> dict[str, Any]:
        try:
            stored = self.storage.get(doc_id)
            self.logger.record(
                op="documents/get", outcome="ok", key=doc_id,
                request_id=request_id,
                intermediate={"revision": stored.revision},
            )
            return self._doc_view(stored)
        except TextIndexError as exc:
            self.logger.record_error(
                op="documents/get", exc=exc, key=doc_id, request_id=request_id)
            raise

    def delete_document(self, doc_id: str, *,
                        request_id: str | None = None) -> None:
        try:
            self.storage.delete(doc_id)
            self.logger.record(
                op="documents/delete", outcome="ok", key=doc_id,
                request_id=request_id)
        except TextIndexError as exc:
            self.logger.record_error(
                op="documents/delete", exc=exc, key=doc_id,
                request_id=request_id)
            raise

    def list_versions(self, doc_id: str, *,
                      request_id: str | None = None) -> list[dict[str, str]]:
        try:
            versions = self.storage.list_versions(doc_id)
            self.logger.record(
                op="documents/versions", outcome="ok", key=doc_id,
                request_id=request_id,
                intermediate={"revisions": len(versions)})
            return versions
        except TextIndexError as exc:
            self.logger.record_error(
                op="documents/versions", exc=exc, key=doc_id,
                request_id=request_id)
            raise

    # --- edit ----------------------------------------------------------------

    def edit_document(
        self,
        doc_id: str,
        *,
        start: int,
        end: int,
        replacement: str,
        unit: str = "grapheme",
        base_digest: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if unit not in UNITS:
            raise InvalidUnit(unit)
        try:
            stored = self.storage.get(doc_id)
            if base_digest is not None and base_digest != stored.text_sha256:
                from .errors import DigestMismatch
                raise DigestMismatch(base_digest, stored.text_sha256)
            encoding.ensure_scalar_value(replacement)
            edit = Edit(start=start, end=end, replacement=replacement, unit=unit)
            result: EditResult = apply_edit(
                stored.index, edit,
                normalization=stored.normalization,
                max_clusters=self.settings.max_clusters,
            )
            size = len(result.text.encode("utf-8"))
            if size > self.settings.max_document_bytes:
                raise DocumentTooLarge(size, self.settings.max_document_bytes)
            updated = self.storage.replace(
                doc_id, result.text, result.index,
                expected_digest=stored.text_sha256,
            )
            self.logger.record(
                op="edits/apply", outcome="ok", key=doc_id,
                request_id=request_id,
                intermediate={
                    "unit": unit, "start": start, "end": end,
                    "replacement_codepoints": len(replacement),
                    "base_revision": stored.revision,
                    "new_revision": updated.revision,
                    "new_window_cp": list(result.new_window),
                    "reused_before": result.reused_before,
                    "rebuilt_clusters": result.rebuilt_clusters,
                    "reused_after": result.reused_after,
                    "delta_codepoints": result.delta_codepoints,
                    "delta_bytes": result.delta_bytes,
                },
            )
            return self._doc_view(updated)
        except TextIndexError as exc:
            self.logger.record_error(
                op="edits/apply", exc=exc, key=doc_id, request_id=request_id,
                intermediate={"unit": unit, "start": start, "end": end},
            )
            raise

    # --- queries -------------------------------------------------------------

    def convert(
        self,
        doc_id: str,
        *,
        position: int,
        from_unit: str,
        to_unit: str,
        strict: bool = True,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            stored = self.storage.get(doc_id)
            idx = stored.index
            value, cluster = _convert(
                idx, position, from_unit, to_unit, strict=strict)
            self.logger.record(
                op="positions/convert", outcome="ok", key=doc_id,
                request_id=request_id,
                intermediate={
                    "from": from_unit, "to": to_unit,
                    "in": position, "out": value,
                    "containing_cluster": cluster, "strict": strict,
                },
            )
            return {
                "doc_id": doc_id, "from": from_unit, "to": to_unit,
                "input": position, "output": value,
                "containing_cluster": cluster, "strict": strict,
            }
        except TextIndexError as exc:
            self.logger.record_error(
                op="positions/convert", exc=exc, key=doc_id,
                request_id=request_id,
                intermediate={"from": from_unit, "to": to_unit,
                              "in": position, "strict": strict})
            raise

    def clusters(self, doc_id: str, *, request_id: str | None = None,
                 ) -> dict[str, Any]:
        try:
            stored = self.storage.get(doc_id)
            idx = stored.index
            items = [
                {
                    "cluster": i,
                    "text": idx.cluster_text(i),
                    "codepoint_start": idx.cp_start[i],
                    "codepoint_end": idx.cp_start[i + 1],
                    "byte_start": idx.byte_start[i],
                    "byte_end": idx.byte_start[i + 1],
                }
                for i in range(idx.cluster_count)
            ]
            self.logger.record(
                op="clusters/list", outcome="ok", key=doc_id,
                request_id=request_id,
                intermediate={"clusters": len(items)})
            return {
                "doc_id": doc_id,
                "digest": stored.text_sha256,
                "normalization": stored.normalization,
                "cluster_count": idx.cluster_count,
                "codepoint_count": idx.codepoint_count,
                "byte_count": idx.byte_count,
                "clusters": items,
            }
        except TextIndexError as exc:
            self.logger.record_error(
                op="clusters/list", exc=exc, key=doc_id, request_id=request_id)
            raise

    def validate_index(self, doc_id: str, *,
                       request_id: str | None = None) -> dict[str, Any]:
        """Recompute a fresh index and compare against the stored one."""
        try:
            stored = self.storage.get(doc_id)
            fresh = index_mod.build_index(stored.text)
            same = (list(fresh.cp_start) == list(stored.index.cp_start)
                    and list(fresh.byte_start) == list(stored.index.byte_start))
            self.logger.record(
                op="diagnostics/validate",
                outcome="ok" if same else "computation_failure",
                key=doc_id, request_id=request_id,
                code=None if same else "index_corrupt",
                intermediate={"matches_full_rebuild": same},
                reason=None if same else "stored index differs from rebuild",
            )
            return {"doc_id": doc_id, "matches_full_rebuild": same}
        except TextIndexError as exc:
            self.logger.record_error(
                op="diagnostics/validate", exc=exc, key=doc_id,
                request_id=request_id)
            raise

    # --- helpers -------------------------------------------------------------

    @staticmethod
    def _ingest(raw: bytes | str) -> tuple[str, str]:
        if isinstance(raw, bytes):
            return encoding.decode_utf8(raw), "raw_octets"
        encoding.ensure_scalar_value(raw)
        return raw, "json_string"

    @staticmethod
    def _doc_view(stored: StoredDocument) -> dict[str, Any]:
        idx = stored.index
        return {
            "doc_id": stored.doc_id,
            "revision": stored.revision,
            "normalization": stored.normalization,
            "digest": stored.text_sha256,
            "index_version": DATA_VERSION_IDENTITY,
            "unicode_version": UNICODE_VERSION,
            "byte_count": idx.byte_count,
            "codepoint_count": idx.codepoint_count,
            "cluster_count": idx.cluster_count,
            "text": stored.text,
        }


def _convert(idx: index_mod.TextIndex, position: int, from_unit: str,
             to_unit: str, *, strict: bool) -> tuple[int, int]:
    """Pure conversion; returns (converted position, containing cluster)."""
    units = ("byte", "codepoint", "grapheme")
    if from_unit not in units:
        raise InvalidUnit(from_unit)
    if to_unit not in units:
        raise InvalidUnit(to_unit)

    if from_unit == "grapheme":
        cluster = position
        idx.cluster_to_codepoint(position)  # range check
    elif from_unit == "codepoint":
        cluster = idx.codepoint_to_cluster(position, strict=strict)
    else:
        cluster = idx.byte_to_cluster(position, strict=strict)

    if to_unit == "grapheme":
        value = cluster
    elif to_unit == "codepoint":
        if from_unit == "codepoint":
            value = position
        elif from_unit == "grapheme":
            value = idx.cluster_to_codepoint(position)
        else:
            value = idx.byte_to_codepoint(position, strict=strict)
    else:  # byte
        if from_unit == "byte":
            value = position
        elif from_unit == "grapheme":
            value = idx.cluster_to_byte(position)
        else:
            value = idx.codepoint_to_byte(position, strict=strict)
    return value, cluster

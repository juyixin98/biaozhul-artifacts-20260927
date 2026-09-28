"""Column service.

The in-memory registry holds the *owned* :class:`ColumnView` instances (which
keep their buffers alive); the SQLite store records their metadata and every
operation transactionally.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.adapters.descriptor import descriptor_to_raw
from app.adapters.importer import import_ipc_stream, import_raw
from app.core.columnview import ColumnView
from app.core.concat import concat
from app.core.layout import RawColumnBuffers
from app.core import types as tt
from app.errors import ErrorCategory, LayoutError
from app.logging_setup import get_logger
from app.store.metadata import MetadataStore
from app.validation.checks import validate

LOG = get_logger("service")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class ColumnRecord:
    column_id: str
    view: ColumnView
    source: str
    parents: list[str]


def _buffer_summary(view: ColumnView) -> list[dict]:
    return [b.to_dict() for b in view.buffer_identities()]


class ColumnService:
    def __init__(self, store: MetadataStore):
        self.store = store
        self._columns: dict[str, ColumnRecord] = {}

    # ------------------------------------------------------------ validate
    def validate_descriptor(self, payload: dict, *, run_id: str, input_fp: str) -> dict:
        op_id = _id("op")
        LOG.info("validate start: op=%s type=%s", op_id, payload.get("type"))
        try:
            raw = descriptor_to_raw(payload)
            report = validate(raw)
            self.store.record_operation(
                op_id=op_id, run_id=run_id, op_type="validate",
                status="ok" if report.ok else "invalid",
                input_fp=input_fp, result_id=None,
                detail={"type": raw.type_name, "length": raw.length,
                        "failure_categories": report.failure_categories},
                error=None,
            )
            self.store.attach_validation(op_id, report.to_dict())
            body = report.to_dict()
            body["op_id"] = op_id
            body["run_id"] = run_id
            LOG.info("validate done: op=%s ok=%s categories=%s",
                     op_id, report.ok, report.failure_categories)
            return body
        except LayoutError as exc:
            self._record_failure(op_id, run_id, "validate", input_fp, exc)
            raise

    # -------------------------------------------------------------- import
    def import_descriptor(self, payload: dict, *, run_id: str, input_fp: str) -> dict:
        op_id = _id("op")
        column_id = _id("col")
        LOG.info("import start: op=%s column=%s", op_id, column_id)
        raw = descriptor_to_raw(payload)
        report = validate(raw)
        self.store.attach_validation(op_id, report.to_dict())
        if not report.ok:
            self.store.record_audit(
                op_id=op_id, run_id=run_id, op_type="import", status="invalid",
                input_fp=input_fp,
                detail={"type": raw.type_name, "length": raw.length,
                        "failure_categories": report.failure_categories},
                error={"categories": report.failure_categories},
            )
            first_category = report.failures[0].category
            try:
                category = ErrorCategory(first_category)
            except ValueError:
                category = ErrorCategory.MALFORMED_PAYLOAD
            raise LayoutError(
                category,
                "import rejected: buffer validation failed",
                detail={"op_id": op_id, "failure_categories": report.failure_categories,
                        "checks": [c.to_dict() for c in report.failures]},
            )

        result = import_raw(raw, validation_values=report.values)
        view = result.view

        def txn_work() -> None:
            self._columns[column_id] = ColumnRecord(column_id, view, source="descriptor", parents=[])
            self.store.upsert_column(
                column_id=column_id, type_name=view.type_name,
                logical_offset=view.offset, logical_length=view.length,
                null_count=view.logical_null_count(), source="descriptor",
                parent_ids=[], buffers=_buffer_summary(view), run_id=run_id,
            )

        self.store.record_operation(
            op_id=op_id, run_id=run_id, op_type="import", status="ok",
            input_fp=input_fp, result_id=column_id,
            detail={"type": view.type_name, "length": view.length,
                    "buffers": _buffer_summary(view),
                    "cross_check": result.evidence.to_dict()},
            error=None, work=txn_work,
        )
        LOG.info("import done: op=%s column=%s values=%s", op_id, column_id,
                 result.evidence.pyarrow_values[:8])
        return self._column_body(column_id, op_id, run_id,
                                 extra={"import_evidence": result.evidence.to_dict()})

    def import_ipc(self, message: bytes, column_index: int, *,
                   run_id: str, input_fp: str) -> dict:
        op_id = _id("op")
        column_id = _id("col")
        result = import_ipc_stream(message, column_index)
        view = result.view

        def txn_work() -> None:
            self._columns[column_id] = ColumnRecord(column_id, view, source="ipc", parents=[])
            self.store.upsert_column(
                column_id=column_id, type_name=view.type_name,
                logical_offset=view.offset, logical_length=view.length,
                null_count=view.logical_null_count(), source="ipc",
                parent_ids=[], buffers=_buffer_summary(view), run_id=run_id,
            )

        self.store.record_operation(
            op_id=op_id, run_id=run_id, op_type="import_ipc", status="ok",
            input_fp=input_fp, result_id=column_id,
            detail={"type": view.type_name, "length": view.length,
                    "buffers": _buffer_summary(view),
                    "cross_check": result.evidence.to_dict()},
            error=None, work=txn_work,
        )
        return self._column_body(column_id, op_id, run_id,
                                 extra={"import_evidence": result.evidence.to_dict()})

    # --------------------------------------------------------------- slice
    def slice_column(self, column_id: str, offset: int, length: int, *,
                     run_id: str, input_fp: str) -> dict:
        op_id = _id("op")
        parent = self._require(column_id)
        child_view = parent.view.slice(offset, length)
        child_id = _id("col")
        parent_buffers = {b.name: b.address for b in parent.view.buffer_identities()}
        child_buffers = {b.name: b.address for b in child_view.buffer_identities()}
        shared = {k: parent_buffers.get(k) == child_buffers.get(k)
                  for k in parent_buffers.keys() | child_buffers.keys()}

        def txn_work() -> None:
            self._columns[child_id] = ColumnRecord(
                child_id, child_view, source="slice", parents=[column_id])
            self.store.upsert_column(
                column_id=child_id, type_name=child_view.type_name,
                logical_offset=child_view.offset, logical_length=child_view.length,
                null_count=child_view.logical_null_count(), source="slice",
                parent_ids=[column_id], buffers=_buffer_summary(child_view), run_id=run_id,
            )

        detail = {
            "parent_id": column_id, "offset": offset, "length": length,
            "buffers_shared_with_parent": shared,
            "copied_bytes": {"total": 0}, "zero_copy": True,
            "null_flags": [child_view.is_null(i) for i in range(child_view.length)],
        }
        self.store.record_operation(
            op_id=op_id, run_id=run_id, op_type="slice", status="ok",
            input_fp=input_fp, result_id=child_id, detail=detail, error=None, work=txn_work,
        )
        LOG.info("slice done: %s[%d:%d] -> %s zero_copy=%s nulls=%s",
                 column_id, offset, offset + length, child_id, shared,
                 detail["null_flags"])
        body = self._column_body(child_id, op_id, run_id)
        body["slice"] = detail
        return body

    # -------------------------------------------------------------- concat
    def concat_columns(self, column_ids: list[str], target_type: str | None, *,
                       run_id: str, input_fp: str) -> dict:
        op_id = _id("op")
        if not column_ids:
            raise LayoutError(ErrorCategory.MALFORMED_PAYLOAD,
                              "concat requires a non-empty column_ids list")
        records = [self._require(cid) for cid in column_ids]
        views = [rec.view for rec in records]
        type_names = sorted({v.type_name for v in views})
        if target_type is None and len(type_names) > 1:
            exc = LayoutError(
                ErrorCategory.TYPE_MISMATCH,
                "inputs have different types; provide target_type for explicit cast",
                detail={"types": type_names},
            )
            self._record_failure(op_id, run_id, "concat", input_fp, exc)
            raise exc

        result = concat(views, target_type=target_type)
        child_id = _id("col")

        def txn_work() -> None:
            self._columns[child_id] = ColumnRecord(
                child_id, result.view, source="concat", parents=list(column_ids))
            self.store.upsert_column(
                column_id=child_id, type_name=result.view.type_name,
                logical_offset=0, logical_length=result.view.length,
                null_count=result.view.logical_null_count(), source="concat",
                parent_ids=list(column_ids),
                buffers=_buffer_summary(result.view), run_id=run_id,
            )

        self.store.record_operation(
            op_id=op_id, run_id=run_id,
            op_type="concat_cast" if target_type else "concat",
            status="ok", input_fp=input_fp, result_id=child_id,
            detail={"inputs": column_ids, "input_types": type_names,
                    "copy_report": result.report.to_dict()},
            error=None, work=txn_work,
        )
        LOG.info("concat done: %s -> %s copied=%s bytes", column_ids, child_id,
                 result.report.total_bytes)
        body = self._column_body(child_id, op_id, run_id)
        body["concat"] = {"inputs": column_ids, "copy_report": result.report.to_dict()}
        return body

    # -------------------------------------------------------------- lookup
    def get_column(self, column_id: str, *, run_id: str) -> dict:
        rec = self._require(column_id)
        return self._column_body(column_id, None, run_id, record=rec)

    def list_columns(self) -> list[dict]:
        return [
            {"column_id": cid, "type": rec.view.type_name, "offset": rec.view.offset,
             "length": rec.view.length, "null_count": rec.view.logical_null_count(),
             "source": rec.source, "parents": rec.parents}
            for cid, rec in self._columns.items()
        ]

    def drop_column(self, column_id: str, *, run_id: str) -> dict:
        self._require(column_id)
        self.store.delete_column(column_id)
        rec = self._columns.pop(column_id)
        LOG.info("dropped column %s; view owners released after response build", column_id)
        return {"dropped": column_id, "type": rec.view.type_name,
                "length": rec.view.length}

    # -------------------------------------------------------------- internals
    def _require(self, column_id: str) -> ColumnRecord:
        rec = self._columns.get(column_id)
        if rec is None:
            raise LayoutError(ErrorCategory.NOT_FOUND, f"column {column_id!r} not found")
        return rec

    def _record_failure(self, op_id: str, run_id: str, op_type: str,
                        input_fp: str | None, exc: LayoutError) -> None:
        self.store.record_audit(
            op_id=op_id, run_id=run_id, op_type=op_type, status="error",
            input_fp=input_fp, detail={"op": op_type}, error=exc.to_dict(),
        )
        LOG.warning("operation failed: op=%s category=%s msg=%s",
                    op_id, exc.category.value, exc.message)

    def _column_body(self, column_id: str, op_id: str | None, run_id: str, *,
                     extra: dict | None = None, record: ColumnRecord | None = None) -> dict:
        rec = record or self._columns[column_id]
        v = rec.view
        body: dict = {
            "column_id": column_id,
            "op_id": op_id,
            "run_id": run_id,
            "type": v.type_name,
            "offset": v.offset,
            "length": v.length,
            "null_count": v.logical_null_count(),
            "null_flags": [v.is_null(i) for i in range(v.length)],
            "values": v.to_pylist(),
            "buffers": _buffer_summary(v),
            "source": rec.source,
            "parents": rec.parents,
        }
        if extra:
            body.update(extra)
        return body

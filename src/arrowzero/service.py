"""Service layer: orchestrates adapters, kernel, metadata transactions and logs.

The service owns the in-memory :class:`~arrowzero.kernel.view.ColumnView`
registry (a bounded handle table) and wraps every operation so that:

* expected validation/format/type errors commit an audit row with status
  ``rejected`` and the precise failure category, but register no array;
* unexpected errors roll business work back and leave a ``failed`` row;
* success commits business state and audit row atomically.
"""

from __future__ import annotations

import base64
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from arrowzero.adapters import (
    FormatError,
    describe_raw_violations,
    export_ipc_stream,
    fingerprint_buffers,
    import_ipc_stream,
    import_pylist,
    import_raw_buffers,
)
from arrowzero.kernel.concat import CastError, concat
from arrowzero.kernel.checks import ValidationError
from arrowzero.kernel.view import ColumnView
from arrowzero.metadata import (
    STATUS_COMMITTED,
    STATUS_FAILED,
    STATUS_REJECTED,
    MetadataStore,
)
from arrowzero.observability import RunLogger
from arrowzero.versions import runtime_versions


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _handle() -> str:
    return f"arr_{uuid.uuid4().hex[:12]}"


class NotFound(KeyError):
    pass


class Registry:
    """Bounded, thread-safe handle -> ColumnView table."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._views: OrderedDict[str, ColumnView] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, view: ColumnView) -> str:
        with self._lock:
            handle = _handle()
            self._views[handle] = view
            while len(self._views) > self.capacity:
                self._views.popitem(last=False)
            return handle

    def get(self, handle: str) -> ColumnView:
        with self._lock:
            try:
                return self._views[handle]
            except KeyError:
                raise NotFound(f"no array registered for handle {handle!r}") from None

    def __len__(self) -> int:
        with self._lock:
            return len(self._views)


@dataclass
class OperationResult:
    status: str
    run_id: str
    op_id: int | None
    payload: dict[str, Any]
    error: dict[str, Any] | None = None


class ViewService:
    def __init__(
        self,
        store: MetadataStore,
        registry: Registry,
        logger: RunLogger,
        *,
        run_purpose: str = "api",
    ) -> None:
        self.store = store
        self.registry = registry
        self.logger = logger
        self.run_purpose = run_purpose

    # ------------------------------------------------------------------

    def _begin(self, run_id: str | None, step: str) -> str:
        run_id = run_id or new_run_id()
        self.store.ensure_run(run_id, self.run_purpose, runtime_versions())
        self.logger.emit(run_id, "started", step=step, verdict="pending", detail={})
        return run_id

    def _finish(
        self, run_id: str, step: str, name: str, payload: dict[str, Any]
    ) -> OperationResult:
        with self.store.transaction() as cur:
            cur.execute(
                """INSERT INTO operations(run_id, name, status, error_code, detail)
                   VALUES (?, ?, 'committed', NULL, ?)""",
                (run_id, name, _json(payload)),
            )
            op_id = int(cur.lastrowid)
        self.logger.emit(
            run_id,
            "completed",
            step=step,
            verdict="committed",
            detail={"op_id": op_id, **payload},
        )
        return OperationResult(STATUS_COMMITTED, run_id, op_id, payload)

    def _reject(
        self,
        run_id: str,
        step: str,
        name: str,
        exc: Exception,
        *,
        category: str,
        extra: dict[str, Any] | None = None,
    ) -> OperationResult:
        detail: dict[str, Any] = {"error": category, "message": str(exc)}
        if extra:
            detail.update(extra)
        op_id = self.store.record_operation(
            run_id=run_id,
            name=name,
            status=STATUS_REJECTED,
            detail=detail,
            error_code=category,
        )
        self.logger.emit(
            run_id,
            "rejected",
            step=step,
            verdict=category,
            detail={"op_id": op_id, **detail},
        )
        return OperationResult(
            STATUS_REJECTED,
            run_id,
            op_id,
            payload={"status": "rejected"},
            error={"code": category, "message": str(exc), **(extra or {})},
        )

    def _fail(self, run_id: str, step: str, name: str, exc: BaseException) -> OperationResult:
        # Separate transaction: any caller-side transaction has been rolled back.
        detail = {"error": type(exc).__name__, "message": str(exc)}
        op_id = self.store.record_operation(
            run_id=run_id,
            name=name,
            status=STATUS_FAILED,
            detail=detail,
            error_code=type(exc).__name__,
        )
        self.logger.emit(
            run_id,
            "failed",
            step=step,
            verdict=type(exc).__name__,
            detail={"op_id": op_id, **detail},
        )
        return OperationResult(
            STATUS_FAILED,
            run_id,
            op_id,
            payload={"status": "failed"},
            error={"code": type(exc).__name__, "message": str(exc)},
        )

    # ----- operations -----------------------------------------------------

    def import_array(
        self,
        request: dict[str, Any],
        *,
        run_id: str | None = None,
    ) -> OperationResult:
        run_id = self._begin(run_id, "import")
        fmt = request.get("format", "pylist")
        try:
            if fmt == "pylist":
                if not request.get("type"):
                    raise FormatError("pylist import requires string field 'type'")
                view, evidence = import_pylist(request.get("values", []), request["type"])
            elif fmt == "ipc_stream":
                payload_b64 = request.get("payload")
                if not isinstance(payload_b64, str):
                    raise FormatError("ipc_stream import needs string field 'payload' (base64)")
                view, evidence = import_ipc_stream(
                    base64.b64decode(payload_b64, validate=True)
                )
            elif fmt == "raw_buffers":
                view, evidence = import_raw_buffers(request)
            else:
                raise FormatError(f"unknown import format {fmt!r}")
        except ValidationError as exc:
            return self._reject(
                run_id, "import", "import", exc,
                category="VALIDATION",
                extra={"violations": exc.to_dicts()},
            )
        except FormatError as exc:
            return self._reject(run_id, "import", "import", exc, category="FORMAT")
        except Exception as exc:  # unexpected → failed, not success
            return self._fail(run_id, "import", "import", exc)

        payload = {
            "format": fmt,
            "evidence": evidence,
        }
        try:
            with self.store.transaction() as cur:
                handle = self.registry.put(view)
                self.store.register_array(
                    cur,
                    handle=handle,
                    run_id=run_id,
                    type_name=str(view.type),
                    length=view.length,
                    logical_offset=view.offset,
                    null_count=view.count_nulls(),
                    origin=view.origin,
                    import_format=fmt,
                    fingerprints=fingerprint_buffers(view),
                )
                cur.execute(
                    """INSERT INTO operations(run_id, name, status, error_code, detail)
                       VALUES (?, 'import', 'committed', NULL, ?)""",
                    (run_id, _json({"handle": handle, **payload})),
                )
                op_id = int(cur.lastrowid)
        except Exception as exc:
            return self._fail(run_id, "import", "import", exc)
        self.logger.emit(
            run_id, "completed", step="import", verdict="committed",
            detail={"op_id": op_id, "handle": handle, **payload},
        )
        return OperationResult(
            STATUS_COMMITTED, run_id, op_id, {"handle": handle, **payload}
        )

    def slice_array(
        self,
        handle: str,
        offset: int,
        length: int | None,
        *,
        run_id: str | None = None,
    ) -> OperationResult:
        run_id = self._begin(run_id, "slice")
        try:
            view = self.registry.get(handle)
            sliced = view.slice(offset, length)
        except NotFound as exc:
            return self._reject(run_id, "slice", "slice", exc, category="NOT_FOUND")
        except IndexError as exc:
            return self._reject(run_id, "slice", "slice", exc, category="SLICE_RANGE")
        except Exception as exc:
            return self._fail(run_id, "slice", "slice", exc)

        # Zero-copy evidence: source and slice buffers are identical objects.
        same = [
            (a is b)
            for a, b in zip(view.buffers, sliced.buffers)
        ]
        payload = {
            "source_handle": handle,
            "zero_copy": all(same),
            "buffer_identity": dict(
                zip(sliced._buffer_names(), same)
            ),
            "logical_offset": sliced.offset,
            "length": sliced.length,
            "null_count": sliced.count_nulls(),
            "copied_bytes": 0,
        }
        with self.store.transaction() as cur:
            out_handle = self.registry.put(sliced)
            self.store.register_array(
                cur,
                handle=out_handle,
                run_id=run_id,
                type_name=str(sliced.type),
                length=sliced.length,
                logical_offset=sliced.offset,
                null_count=sliced.count_nulls(),
                origin=sliced.origin,
                import_format="slice",
                fingerprints=fingerprint_buffers(sliced),
            )
            return self._commit(cur, run_id, "slice", out_handle, payload)

    def concat_arrays(
        self,
        handles: list[str],
        cast_to: str | None,
        *,
        run_id: str | None = None,
    ) -> OperationResult:
        run_id = self._begin(run_id, "concat")
        try:
            views = [self.registry.get(h) for h in handles]
        except NotFound as exc:
            return self._reject(run_id, "concat", "concat", exc, category="NOT_FOUND")
        try:
            merged, ledger = concat(views, cast_to=cast_to)
        except CastError as exc:
            return self._reject(
                run_id, "concat", "concat", exc, category="TYPE_MISMATCH",
                extra={
                    "input_types": [str(v.type) for v in views],
                    "cast_to": cast_to,
                    "hint": "specify cast_to for an explicit conversion",
                },
            )
        except ValueError as exc:
            return self._reject(run_id, "concat", "concat", exc, category="BAD_REQUEST")
        except Exception as exc:
            return self._fail(run_id, "concat", "concat", exc)

        payload = {
            "source_handles": handles,
            "cast_to": cast_to,
            "type": str(merged.type),
            "length": merged.length,
            "null_count": merged.count_nulls(),
            "copy": ledger.as_dict(),
            "values": merged.to_pylist(),
        }
        with self.store.transaction() as cur:
            handle = self.registry.put(merged)
            self.store.register_array(
                cur,
                handle=handle,
                run_id=run_id,
                type_name=str(merged.type),
                length=merged.length,
                logical_offset=merged.offset,
                null_count=merged.count_nulls(),
                origin=merged.origin,
                import_format="concat",
                fingerprints=fingerprint_buffers(merged),
            )
            return self._commit(cur, run_id, "concat", handle, payload)

    def _commit(self, cur, run_id, name, handle, extra: dict[str, Any]) -> OperationResult:
        cur.execute(
            """INSERT INTO operations(run_id, name, status, error_code, detail)
               VALUES (?, ?, 'committed', NULL, ?)""",
            (run_id, name, _json({"handle": handle, **extra})),
        )
        op_id = int(cur.lastrowid)
        self.logger.emit(
            run_id, "completed", step=name, verdict="committed",
            detail={"op_id": op_id, "handle": handle, **extra},
        )
        return OperationResult(
            STATUS_COMMITTED, run_id, op_id, {"handle": handle, **extra}
        )

    def export_array(self, handle: str, *, run_id: str | None = None) -> OperationResult:
        run_id = self._begin(run_id, "export")
        try:
            view = self.registry.get(handle)
        except NotFound as exc:
            return self._reject(run_id, "export", "export", exc, category="NOT_FOUND")
        payload = {
            "handle": handle,
            "format": "ipc_stream",
            "payload": base64.b64encode(export_ipc_stream(view)).decode("ascii"),
            "describe": view.describe(),
        }
        return self._finish(run_id, "export", "export", payload)

    def get_values(self, handle: str, *, run_id: str | None = None) -> OperationResult:
        run_id = self._begin(run_id, "values")
        try:
            view = self.registry.get(handle)
        except NotFound as exc:
            return self._reject(run_id, "values", "values", exc, category="NOT_FOUND")
        return self._finish(
            run_id, "values", "values",
            {"handle": handle, "values": view.to_pylist(), "describe": view.describe()},
        )

    def validate_descriptor(self, descriptor: dict[str, Any], *, run_id: str | None = None):
        run_id = self._begin(run_id, "validate")
        try:
            violations = describe_raw_violations(descriptor)
        except FormatError as exc:
            return self._reject(run_id, "validate", "validate", exc, category="FORMAT")
        accepted = not violations
        payload = {
            "accepted": accepted,
            "violations": violations,
            "violation_codes": [v["code"] for v in violations],
        }
        return self._finish(
            run_id, "validate", "validate", payload
        )


def _json(obj: Any) -> str:
    import json

    return json.dumps(obj, default=str, sort_keys=True)

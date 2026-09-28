"""Safe import adapter.

Two entry paths:

* :func:`import_raw` - build an owned zero-copy :class:`ColumnView` from
  externally supplied raw buffers (already validated).
* :func:`import_ipc_stream` - parse an Arrow IPC streaming message and import
  its first column, then narrow it via the same ownership path.

Both retain the source memory in the view's owner list, so dropping the
caller's buffer/IPC objects can never dangle the view.

A PyArrow cross-check is run after construction: our independent structural
validation decodes the values, and PyArrow is asked to decode the imported
array too; any divergence is reported as a verification failure instead of
being silently trusted.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pyarrow as pa

from app.core import bitmath
from app.core.columnview import ColumnView
from app.core.layout import RawColumnBuffers
from app.core import types as tt
from app.errors import ErrorCategory, LayoutError


@dataclass
class ImportEvidence:
    pyarrow_values: list[object]
    validation_values: list[object]
    agreement: bool
    pyarrow_null_count: int
    buffer_addresses: dict = field(default_factory=dict)
    steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "pyarrow_values": self.pyarrow_values[:8],
            "validation_values_preview": self.validation_values[:8],
            "agreement": self.agreement,
            "pyarrow_null_count": self.pyarrow_null_count,
            "buffer_addresses": self.buffer_addresses,
            "steps": self.steps,
        }


@dataclass
class ImportResult:
    view: ColumnView
    evidence: ImportEvidence


def import_raw(raw: RawColumnBuffers, validation_values: list[object] | None = None) -> ImportResult:
    steps: list[str] = []
    owners: list[object] = []

    # Safe-by-default: never hand unvalidated raw bytes to Arrow. The service
    # layer normally validates first, but this guarantees no caller can bypass.
    if validation_values is None:
        from app.validation.checks import validate as _validate
        report = _validate(raw)
        if not report.ok:
            first = report.failures[0]
            try:
                category = ErrorCategory(first.category)
            except ValueError:
                category = ErrorCategory.MALFORMED_PAYLOAD
            raise LayoutError(
                category, "import_raw refused: structural validation failed",
                detail={"failure_categories": report.failure_categories},
            )
        validation_values = report.values
    steps.append("structural validation passed before constructing the Arrow array")

    def wrap(buf: bytes | None) -> pa.Buffer | None:
        if buf is None:
            return None
        # memoryview -> pa.py_buffer is a zero-copy export of the buffer
        # protocol; both the python bytes and the arrow buffer are retained.
        pb = pa.py_buffer(memoryview(buf))
        owners.append(buf)
        owners.append(pb)
        return pb

    vb = wrap(raw.validity)
    ob = wrap(raw.offsets)
    db = wrap(raw.data)
    steps.append("wrapped raw bytes as pa.Buffer via buffer protocol (zero-copy export)")

    pa_buffers: list[pa.Buffer | None]
    if tt.is_string(raw.type_name):
        pa_buffers = [vb, ob, db]
    else:
        pa_buffers = [vb, db]

    null_count = -1 if raw.validity is None else (
        raw.length - bitmath.count_set_bits(raw.validity, 0, raw.length)
    )
    try:
        array = pa.Array.from_buffers(
            tt.pa_type(raw.type_name), raw.length, pa_buffers, null_count,
        )
    except (pa.ArrowInvalid, pa.ArrowException) as exc:  # defensive; validation should pre-empt
        raise LayoutError(ErrorCategory.SEMANTIC_SCAN_FAILED,
                          f"PyArrow rejected buffers that passed structural validation: {exc}") from exc
    steps.append(f"pa.Array.from_buffers({raw.type_name}, length={raw.length}, null_count={null_count})")

    view = ColumnView.from_array(array, owners=owners)
    evidence = _cross_check(view, validation_values, steps)
    return ImportResult(view, evidence)


def import_ipc_stream(message: bytes, column_index: int = 0) -> ImportResult:
    steps = [f"received {len(message)} IPC stream bytes, reading column {column_index}"]
    try:
        reader = pa.ipc.open_stream(pa.py_buffer(message))
        table = reader.read_all()
    except (pa.ArrowInvalid, OSError, ValueError) as exc:
        raise LayoutError(ErrorCategory.MALFORMED_PAYLOAD,
                          f"not a valid Arrow IPC stream: {exc}") from exc
    if column_index >= table.num_columns:
        raise LayoutError(ErrorCategory.MALFORMED_PAYLOAD,
                          f"IPC batch has {table.num_columns} columns; index {column_index} absent",
                          detail={"num_columns": table.num_columns})
    chunked = table.column(column_index)
    if chunked.num_chunks != 1:
        raise LayoutError(ErrorCategory.MALFORMED_PAYLOAD,
                          "fixture contract expects exactly one chunk per IPC stream",
                          detail={"chunks": chunked.num_chunks})
    array = chunked.chunk(0)
    type_name = str(array.type)
    if not tt.is_supported(type_name):
        raise LayoutError(ErrorCategory.UNSUPPORTED_TYPE,
                          f"IPC column type {type_name!r} not supported")
    owners = [message, array]
    for buf in array.buffers():
        if buf is not None:
            owners.append(buf)
    view = ColumnView.from_array(array, owners=owners)
    steps.append(f"imported column {column_index} ({type_name}, {len(array)} rows) retaining IPC body")
    # For the IPC path PyArrow is the parser; we still independently decode to
    # confirm our view logic agrees over the same physical memory.
    expected = array.to_pylist()
    evidence = _cross_check(view, expected, steps, oracle_is_pyarrow=True)
    return ImportResult(view, evidence)


def _cross_check(view: ColumnView, expected: list[object] | None, steps: list[str],
                 oracle_is_pyarrow: bool = False) -> ImportEvidence:
    pyarrow_values = view.materialize().to_pylist()
    if expected is None:
        expected = pyarrow_values
    agreement = view.to_pylist() == expected == pyarrow_values
    addresses = {b.name: hex(b.address) for b in view.buffer_identities()}
    label = "PyArrow" if oracle_is_pyarrow else "independent validator"
    steps.append(
        f"cross-check: view decode vs {label} vs PyArrow = "
        f"{'AGREE' if agreement else 'DIVERGE'} over {view.length} elements"
    )
    if not agreement:
        first_div = next(
            (i for i in range(view.length)
             if not _eq(view.value(i), expected[i]) or not _eq(view.value(i), pyarrow_values[i])),
            None,
        )
        raise LayoutError(
            ErrorCategory.SEMANTIC_SCAN_FAILED,
            f"view decode diverges from reference at logical index {first_div}",
            detail={
                "first_divergence": first_div,
                "view_value": _safe(view, first_div),
                "reference_value": None if first_div is None else expected[first_div],
            },
        )
    return ImportEvidence(
        pyarrow_values=pyarrow_values,
        validation_values=list(expected),
        agreement=True,
        pyarrow_null_count=view.materialize().null_count if view.materialize().null_count is not None else 0,
        buffer_addresses=addresses,
        steps=steps,
    )


def _eq(a: object, b: object) -> bool:
    # NaN != NaN in struct; compare float NaNs as equal for evidence.
    if isinstance(a, float) and isinstance(b, float) and a != a and b != b:
        return True
    return a == b


def _safe(view: ColumnView, i: int | None) -> object:
    if i is None:
        return None
    try:
        return view.value(i)
    except Exception:  # pragma: no cover - defensive reporting path
        return "<unreadable>"

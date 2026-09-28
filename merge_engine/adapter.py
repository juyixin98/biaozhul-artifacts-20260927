"""Source format adapter.

Accepts three local, explicit input shapes and normalizes them to
:class:`~merge_engine.contract.SourceRow`:

* ``{"format": "arrow", "table": <pyarrow.Table>}`` - in-process
* ``{"format": "arrow-ipc", "data": b"..."}``       - Arrow file/stream bytes
* ``{"format": "records", "records": [{...}, ...]}``- JSON-able Python rows

This is the only place that knows about PyArrow. Downstream modules operate on
plain dict rows, which is also what the independent test oracle uses.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from typing import Any

from .contract import SourceRow
from .errors import ROW_LIMIT_CODE, SOURCE_SCHEMA_CODE, UNSUPPORTED_VALUE_CODE, InputError
from .utils import fingerprint


def load_source_rows(payload: Any, *, max_rows: int) -> tuple[list[SourceRow], str]:
    """Return canonical rows + an order-independent fingerprint.

    Raises InputError for every malformed-input / unsupported-type case.
    Row-count limits are enforced here (category INPUT_ERROR, code
    ROW_LIMIT_EXCEEDED - the boundary is a request-level resource cap).
    """
    if not isinstance(payload, Mapping):
        raise InputError(
            SOURCE_SCHEMA_CODE,
            "'source' must be an object with a 'format' field",
            {"got": type(payload).__name__},
        )
    fmt = payload.get("format")
    if fmt == "records":
        rows, columns = _from_records(payload.get("records"))
    elif fmt == "arrow":
        rows, columns = _from_arrow_obj(payload.get("table"))
    elif fmt == "arrow-ipc":
        rows, columns = _from_arrow_ipc(payload.get("data"))
    else:
        raise InputError(
            SOURCE_SCHEMA_CODE,
            "source.format must be one of 'records', 'arrow', 'arrow-ipc'",
            {"got": fmt},
        )

    if len(rows) > max_rows:
        raise InputError(
            ROW_LIMIT_CODE,
            f"source has {len(rows)} rows, exceeding max_source_rows={max_rows}",
            {"source_rows": len(rows), "max_source_rows": max_rows},
        )

    # An empty batch (records=[]) legitimately has no derivable columns; it is
    # a valid no-op. A non-empty batch with rows always yields columns.
    if columns and len(set(columns)) != len(columns):
        raise InputError(SOURCE_SCHEMA_CODE, "source column names must be unique", {"columns": columns})

    fp = fingerprint([dict(r.values) for r in rows])
    return rows, fp


def _from_records(raw: Any) -> tuple[list[SourceRow], list[str]]:
    if not isinstance(raw, list):
        raise InputError(SOURCE_SCHEMA_CODE, "'records' must be a list of objects")
    columns: list[str] = []
    seen: set[str] = set()
    rows: list[SourceRow] = []
    for i, rec in enumerate(raw):
        if not isinstance(rec, Mapping):
            raise InputError(
                SOURCE_SCHEMA_CODE,
                f"records[{i}] must be an object",
                {"index": i, "got": type(rec).__name__},
            )
        for col in rec:
            if col not in seen:
                seen.add(col)
                columns.append(col)
        rows.append(SourceRow(index=i, values={k: _json_safe(v, i, k) for k, v in rec.items()}))
    return rows, columns


def _json_safe(v: Any, row: int, col: str) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (list, tuple)):
        return [_json_safe(x, row, col) for x in v]
    if isinstance(v, Mapping):
        return {str(k): _json_safe(x, row, col) for k, x in v.items()}
    if hasattr(v, "as_py"):  # e.g. Decimal-like wrappers
        return _json_safe(v.as_py(), row, col)
    raise InputError(
        UNSUPPORTED_VALUE_CODE,
        f"records[{row}][{col!r}] has unsupported type {type(v).__name__}",
        {"index": row, "column": col, "type": type(v).__name__},
    )


def _from_arrow_obj(obj: Any) -> tuple[list[SourceRow], list[str]]:
    try:
        import pyarrow as pa
    except ImportError as exc:  # pragma: no cover - dependency pinned
        raise InputError(
            UNSUPPORTED_VALUE_CODE, "pyarrow is required for format='arrow'"
        ) from exc
    if not isinstance(obj, pa.Table):
        raise InputError(
            SOURCE_SCHEMA_CODE,
            "source.table must be a pyarrow.Table",
            {"got": type(obj).__name__},
        )
    return _arrow_table_to_rows(obj)


def _from_arrow_ipc(data: Any) -> tuple[list[SourceRow], list[str]]:
    try:
        import pyarrow as pa
    except ImportError as exc:  # pragma: no cover
        raise InputError(
            UNSUPPORTED_VALUE_CODE, "pyarrow is required for format='arrow-ipc'"
        ) from exc
    if isinstance(data, str):
        data = data.encode("latin1")  # bytes may arrive as latin1 string
    if not isinstance(data, (bytes, bytearray)):
        raise InputError(
            SOURCE_SCHEMA_CODE,
            "source.data must be Arrow IPC bytes",
            {"got": type(data).__name__},
        )
    try:
        reader = pa.ipc.open_stream(io.BytesIO(bytes(data)))
        table = reader.read_all()
    except pa.ArrowInvalid:
        try:
            reader = pa.ipc.open_file(io.BytesIO(bytes(data)))
            table = reader.read_all()
        except pa.ArrowInvalid as exc:
            raise InputError(
                SOURCE_SCHEMA_CODE,
                "source.data is neither an Arrow IPC stream nor file",
            ) from exc
    return _arrow_table_to_rows(table)


def _arrow_table_to_rows(table: Any) -> tuple[list[SourceRow], list[str]]:
    import pyarrow as pa

    columns = [f.name for f in table.schema]
    if len(set(columns)) != len(columns):
        raise InputError(SOURCE_SCHEMA_CODE, "source column names must be unique", {"columns": columns})

    # Reject unsupported types up front with a precise error.
    for field_ in table.schema:
        t = field_.type
        if pa.types.is_decimal(t):
            raise InputError(
                UNSUPPORTED_VALUE_CODE,
                f"column {field_.name!r} has unsupported type {t}; "
                "cast to string or double in Arrow before sending",
                {"column": field_.name, "type": str(t)},
            )
        if pa.types.is_dictionary(t):
            # accept dictionaries by decoding
            continue
        if (
            pa.types.is_list(t) or pa.types.is_large_list(t) or pa.types.is_struct(t)
            or pa.types.is_map(t) or pa.types.is_binary(t) or pa.types.is_large_binary(t)
            or pa.types.is_fixed_size_binary(t)
        ):
            raise InputError(
                UNSUPPORTED_VALUE_CODE,
                f"column {field_.name!r} has unsupported type {t}",
                {"column": field_.name, "type": str(t)},
            )

    decoded = table
    if any(pa.types.is_dictionary(f.type) for f in table.schema):
        decoded = table.cast(
            pa.schema(
                [
                    pa.field(f.name, f.type.value_type if pa.types.is_dictionary(f.type) else f.type)
                    for f in table.schema
                ]
            )
        )

    pylist = decoded.to_pylist()
    rows: list[SourceRow] = []
    for i, rec in enumerate(pylist):
        values: dict[str, Any] = {}
        for col in columns:
            values[col] = _convert_arrow_value(rec[col], i, col)
        rows.append(SourceRow(index=i, values=values))
    return rows, columns


def _convert_arrow_value(v: Any, row: int, col: str) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    # timestamps/dates/durations -> ISO strings or ints via as_py first
    if hasattr(v, "as_py"):
        v = v.as_py()
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if hasattr(v, "isoformat"):  # date / datetime
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return [_convert_arrow_value(x, row, col) for x in v]
    raise InputError(
        UNSUPPORTED_VALUE_CODE,
        f"row {row} column {col!r} has unsupported value type {type(v).__name__}",
        {"index": row, "column": col, "type": type(v).__name__},
    )

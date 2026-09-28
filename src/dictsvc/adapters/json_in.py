"""JSON request adapter: maps external JSON onto kernel BatchInput.

Shape problems -> REQUEST_MALFORMED.
Semantic problems (out-of-range indexes, overflow, ...) are deliberately
left to the kernel so every adapter enforces identical semantics.
"""
from __future__ import annotations

from typing import Any

from ..core.errors import DictionaryContainsNull, RequestMalformed, UnsupportedValueType
from ..core.model import BatchInput
from ..core.policy import NULL_SENTINEL, WIDTHS


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _value(v: Any, batch_id: str, code: int) -> object:
    if v is None:
        # NULL must live solely in the validity bitmap, never as a value.
        raise DictionaryContainsNull(
            f"batch {batch_id!r}: dictionary code {code} is null; NULL rows "
            f"must be expressed via indices/valid only",
            batch_id=batch_id, details={"local_code": code})
    if isinstance(v, str):
        return v
    if _is_int(v):
        return v
    raise UnsupportedValueType(
        f"batch {batch_id!r}: dictionary code {code} has unsupported type "
        f"{type(v).__name__}",
        batch_id=batch_id, details={"local_code": code,
                                    "python_type": type(v).__name__})


def parse_batch(raw: Any, ordinal: int) -> BatchInput:
    if not isinstance(raw, dict):
        raise RequestMalformed(
            f"batches[{ordinal}] must be an object",
            details={"position": ordinal})

    batch_id = raw.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id:
        raise RequestMalformed(
            f"batches[{ordinal}].batch_id must be a non-empty string",
            details={"position": ordinal})

    raw_values = raw.get("dictionary")
    if not isinstance(raw_values, list):
        raise RequestMalformed(
            f"batch {batch_id!r}: 'dictionary' must be a list",
            batch_id=batch_id)
    values = tuple(_value(v, batch_id, c) for c, v in enumerate(raw_values))

    raw_indices = raw.get("indices")
    if not isinstance(raw_indices, list):
        raise RequestMalformed(
            f"batch {batch_id!r}: 'indices' must be a list",
            batch_id=batch_id)

    raw_valid = raw.get("valid", None)
    if raw_valid is not None:
        if (not isinstance(raw_valid, list)
                or len(raw_valid) != len(raw_indices)):
            raise RequestMalformed(
                f"batch {batch_id!r}: 'valid' must be a list of the same "
                f"length as 'indices'",
                batch_id=batch_id,
                details={"indices_len": len(raw_indices)})
        if not all(isinstance(x, bool) for x in raw_valid):
            raise RequestMalformed(
                f"batch {batch_id!r}: 'valid' entries must be booleans",
                batch_id=batch_id)

    indices: list[int] = []
    valid: list[bool] = []
    for row, ri in enumerate(raw_indices):
        explicit_valid = raw_valid[row] if raw_valid is not None else None
        if ri is None:
            # Without a bitmap: a JSON null index IS the null marker.
            # With a bitmap: only valid=false is consistent; a null index on
            # an explicitly valid row is contradictory input.
            if explicit_valid is True:
                raise RequestMalformed(
                    f"batch {batch_id!r}: row {row} is marked valid but its "
                    f"index is null",
                    batch_id=batch_id, details={"row": row})
            indices.append(NULL_SENTINEL)
            valid.append(False)
            continue
        if not _is_int(ri):
            raise RequestMalformed(
                f"batch {batch_id!r}: row {row}: index must be an integer "
                f"or null, got {type(ri).__name__}",
                batch_id=batch_id, details={"row": row})
        if explicit_valid is None:
            indices.append(ri)
            valid.append(True)
        else:
            indices.append(ri)
            valid.append(bool(explicit_valid))

    return BatchInput(batch_id=batch_id, values=values,
                      indices=tuple(indices), valid=tuple(valid))


def parse_request(body: Any) -> tuple[list[BatchInput], dict]:
    """Return (batches, options). Shape validation only; kernel does the
    semantic validation."""
    if not isinstance(body, dict):
        raise RequestMalformed("request body must be a JSON object")
    raw_batches = body.get("batches")
    if not isinstance(raw_batches, list) or not raw_batches:
        raise RequestMalformed("'batches' must be a non-empty list")
    batches = [parse_batch(b, i) for i, b in enumerate(raw_batches)]

    options: dict = {}
    tw = body.get("target_width", 8)
    if not _is_int(tw) or tw not in WIDTHS:
        raise RequestMalformed(
            f"target_width must be one of {WIDTHS}", details={"got": tw})
    options["target_width"] = tw

    wp = body.get("width_policy", "reject")
    if wp not in ("reject", "expand"):
        raise RequestMalformed(
            "width_policy must be 'reject' or 'expand'", details={"got": wp})
    options["width_policy"] = wp

    dup = body.get("on_duplicate_values", "merge")
    if dup not in ("merge", "error"):
        raise RequestMalformed(
            "on_duplicate_values must be 'merge' or 'error'",
            details={"got": dup})
    options["on_duplicate_values"] = dup

    run_id = body.get("run_id")
    if run_id is not None and (not isinstance(run_id, str) or not run_id):
        raise RequestMalformed("run_id must be a non-empty string when given")
    options["run_id"] = run_id
    return batches, options

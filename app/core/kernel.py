"""Pure-Python dictionary unification kernel.

The kernel is deliberately independent of FastAPI, PyArrow and SQLite. It works
on plain Python values so the logic can be tested in isolation and the test
oracle does not have to trust any code from this module.

Semantics (see README "支持范围与取舍"):

* NULL is carried by a validity bitmap *only*. A NULL row does not consume a
  local dictionary code and is never encoded as a sentinel value.
* Local index width and the validity bitmap are validated independently.
* Same value under different local codes across (or within) batches is merged;
  different values that happen to share a local code are never merged.
* Global dictionary order is fixed by policy ``typed-ascending-v1``: ascending
  within the request's value type (Unicode code point order for strings).
* Width selection:
    - policy ``auto``   -> smallest of uint8/uint16/uint32 that fits,
                           uint8 reserved for >= 1 distinct value;
    - policy ``strict`` -> the client's exact width, or
                           ``INDEX_WIDTH_OVERFLOW``;
  anything above the hard ceiling (uint32 by default) is
  ``CARDINALITY_LIMIT_EXCEEDED``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..config import (
    AUTO_WIDTHS,
    SORT_POLICY,
    VALUE_TYPES,
    WIDTH_CAPACITY,
)
from .errors import (
    CardinalityLimitError,
    DuplicateBatchIdError,
    DuplicateValueInDictionaryError,
    EmptyRequestError,
    IndexOutOfRangeError,
    IndexWidthOverflowError,
    InvalidIndexError,
    InvalidValidityError,
    MalformedBatchError,
    NullDictionaryEntryError,
    UnsupportedIndexWidthError,
    UnsupportedValueTypeError,
    ValueTypeMismatchError,
)

# Valid request-level index policies / explicit widths.
_VALID_WIDTHS: frozenset[int] = frozenset(WIDTH_CAPACITY)


@dataclass(frozen=True)
class BatchInput:
    """One column batch with a local dictionary and local indices.

    Attributes:
        batch_id: unique within a request.
        dictionary: distinct local values, local code == list position.
        indices: local code per row; ignored at rows where validity is False.
        validity: per-row bitmap; False means NULL (no value semantics).
    """

    batch_id: str
    dictionary: list
    indices: list[int]
    validity: list[bool] = field(default_factory=list)


@dataclass(frozen=True)
class BatchRemap:
    """Per-batch output of the unification."""

    batch_id: str
    # local code -> global code; length == len(local dictionary).
    local_to_global: tuple[int, ...]
    # Global code per row (0 for NULL rows; consult ``validity``).
    global_indices: tuple[int, ...]
    validity: tuple[bool, ...]
    row_count: int
    null_count: int


@dataclass(frozen=True)
class UnifyResult:
    global_dictionary: tuple          # sorted distinct values
    global_value_type: str
    index_width_bits: int
    cardinality: int
    sort_policy: str
    batch_remaps: tuple[BatchRemap, ...]
    stats: dict


def _normalize_scalar(value, value_type: str):
    """Coerce a JSON/Python scalar to the request's declared type.

    Raises a classified error on mismatch. Booleans are handled before ints
    because ``isinstance(True, int)`` is True in Python.
    """
    if value_type == "string":
        if not isinstance(value, str):
            raise ValueTypeMismatchError(
                "dictionary entry is not a string",
                details={"expected": "string", "actual_type": type(value).__name__},
            )
        return value
    if value_type == "bool":
        if not isinstance(value, bool):
            raise ValueTypeMismatchError(
                "dictionary entry is not a bool",
                details={"expected": "bool", "actual_type": type(value).__name__},
            )
        return value
    if value_type == "int64":
        # Reject bools (which are ints in Python), enforce int64 range.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueTypeMismatchError(
                "dictionary entry is not an int64",
                details={"expected": "int64", "actual_type": type(value).__name__},
            )
        if not (-(2**63) <= value <= 2**63 - 1):
            raise ValueTypeMismatchError(
                "integer outside int64 range",
                details={"value": value},
            )
        return value
    if value_type == "double":
        if isinstance(value, bool):
            raise ValueTypeMismatchError(
                "dictionary entry is not a double",
                details={"expected": "double", "actual_type": "bool"},
            )
        if isinstance(value, int):
            value = float(value)
        if not isinstance(value, float):
            raise ValueTypeMismatchError(
                "dictionary entry is not a double",
                details={"expected": "double", "actual_type": type(value).__name__},
            )
        # NaN breaks the fixed total order and equality-based merging; it is
        # explicitly rejected rather than silently sorted somewhere.
        if math.isnan(value):
            raise ValueTypeMismatchError(
                "NaN is not supported as a dictionary value "
                "(breaks fixed ordering and value merging)",
                details={"sort_policy": SORT_POLICY},
            )
        return value
    raise UnsupportedValueTypeError(
        f"unsupported value type {value_type!r}",
        details={"supported": sorted(VALUE_TYPES)},
    )


def _require_int(value, *, what: str, batch_id: str, row: int | None = None):
    """Indices must be real integers (JSON has no separate int/float guarantee
    once parsed into Python, so reject floats even if integral)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidIndexError(
            f"{what} must be an integer",
            details={"batch_id": batch_id, "row": row, "actual_type": type(value).__name__},
        )
    return value


def unify(
    batches: list[BatchInput],
    *,
    value_type: str,
    index_policy: str = "auto",
    target_width: int | None = None,
    max_cardinality: int = 2**32 - 1,
) -> UnifyResult:
    """Unify local dictionaries of several batches into one global dictionary.

    Args:
        batches: non-empty list of batches sharing one value type.
        value_type: one of ``string|int64|double|bool``.
        index_policy: ``auto`` or ``strict``.
        target_width: required width when policy is ``strict``.
        max_cardinality: service hard ceiling.

    Returns:
        UnifyResult with the sorted global dictionary and per-batch remaps.
    """
    if value_type not in VALUE_TYPES:
        raise UnsupportedValueTypeError(
            f"unsupported value type {value_type!r}",
            details={"supported": sorted(VALUE_TYPES)},
        )
    if index_policy not in {"auto", "strict"}:
        raise UnsupportedIndexWidthError(
            f"index_policy must be 'auto' or 'strict', got {index_policy!r}",
        )
    if index_policy == "strict":
        if target_width not in _VALID_WIDTHS:
            raise UnsupportedIndexWidthError(
                "strict policy requires target_width in "
                f"{sorted(_VALID_WIDTHS)}, got {target_width!r}",
            )
    elif target_width is not None:
        # 'auto' derives the width itself; an explicit width here is a client
        # mistake worth flagging rather than silently ignoring.
        raise UnsupportedIndexWidthError(
            "target_width is only valid with index_policy='strict'",
        )

    if not batches:
        raise EmptyRequestError("at least one batch is required")

    seen_ids: set[str] = set()
    total_rows = 0
    total_nulls = 0

    # Step 1: per-batch structural validation + value normalization.
    # Normalized dictionaries are kept so downstream steps compare canonical
    # Python values (e.g. int 1 -> 1.0 for a double request).
    normalized: list[tuple[BatchInput, list]] = []
    for b in batches:
        if not isinstance(b, BatchInput):
            raise MalformedBatchError(
                "each batch must be a BatchInput",
                details={"actual_type": type(b).__name__},
            )
        if not isinstance(b.batch_id, str) or not b.batch_id:
            raise MalformedBatchError("batch_id must be a non-empty string")
        if b.batch_id in seen_ids:
            raise DuplicateBatchIdError(
                "duplicate batch_id", details={"batch_id": b.batch_id}
            )
        seen_ids.add(b.batch_id)

        if not isinstance(b.dictionary, list):
            raise MalformedBatchError(
                "dictionary must be a list", details={"batch_id": b.batch_id}
            )
        if not isinstance(b.indices, list):
            raise MalformedBatchError(
                "indices must be a list", details={"batch_id": b.batch_id}
            )
        if not isinstance(b.validity, list):
            raise MalformedBatchError(
                "validity must be a list", details={"batch_id": b.batch_id}
            )
        if len(b.validity) != len(b.indices):
            raise InvalidValidityError(
                "validity bitmap length must match indices length",
                details={
                    "batch_id": b.batch_id,
                    "validity_len": len(b.validity),
                    "indices_len": len(b.indices),
                },
            )
        if any(not isinstance(v, bool) for v in b.validity):
            raise InvalidValidityError(
                "validity entries must be booleans",
                details={"batch_id": b.batch_id},
            )

        norm_dict = [
            _normalize_scalar(v, value_type) for v in b.dictionary
        ]

        # Duplicate values inside one local dictionary: report the canonical
        # (first) code. Local codes must be unique-by-value for the merge to be
        # unambiguous — we surface this as a classified error and let the
        # adapter canonicalize when the client opted in.
        first_pos: dict = {}
        for pos, v in enumerate(norm_dict):
            if v in first_pos:
                raise DuplicateValueInDictionaryError(
                    "duplicate value in local dictionary",
                    details={
                        "batch_id": b.batch_id,
                        "value_repr": repr(v),
                        "first_code": first_pos[v],
                        "duplicate_code": pos,
                    },
                )
            first_pos[v] = pos

        n = len(b.indices)
        for row, idx in enumerate(b.indices):
            _require_int(idx, what="index", batch_id=b.batch_id, row=row)
            if idx < 0:
                raise InvalidIndexError(
                    "negative index",
                    details={"batch_id": b.batch_id, "row": row, "index": idx},
                )
            # Range is only meaningful where the row is valid (non-NULL).
            # A NULL row carries no value semantics, so its (padding) index is
            # permitted even against an empty dictionary.
            if b.validity[row] and idx >= len(norm_dict):
                raise IndexOutOfRangeError(
                    "index exceeds local dictionary",
                    details={
                        "batch_id": b.batch_id,
                        "row": row,
                        "index": idx,
                        "dictionary_size": len(norm_dict),
                    },
                )

        null_count = sum(1 for v in b.validity if not v)
        total_rows += n
        total_nulls += null_count
        normalized.append((b, norm_dict))

    # Step 2: collect distinct values across batches (same value merges even if
    # local codes differ; different values sharing a code stay distinct because
    # the key is the *value*, not the code).
    distinct: set = set()
    for _, norm_dict in normalized:
        distinct.update(norm_dict)

    cardinality = len(distinct)

    # Step 3: hard ceiling (checked before width selection).
    if cardinality > max_cardinality:
        raise CardinalityLimitError(
            "global cardinality exceeds service maximum",
            details={
                "cardinality": cardinality,
                "max_cardinality": max_cardinality},
        )

    # Step 4: fixed-order global dictionary.
    global_dictionary = sorted(distinct)  # typed-ascending-v1
    global_code: dict = {v: i for i, v in enumerate(global_dictionary)}

    # Step 5: width selection.
    if index_policy == "strict":
        width = target_width
        capacity = WIDTH_CAPACITY[width]
        if cardinality > capacity:
            raise IndexWidthOverflowError(
                "cardinality does not fit requested index width",
                details={
                    "cardinality": cardinality,
                    "requested_width": width,
                    "capacity": capacity,
                },
            )
    else:
        width = _auto_width(cardinality)

    # Step 6: per-batch remap.
    remaps: list[BatchRemap] = []
    for b, norm_dict in normalized:
        l2g = tuple(global_code[v] for v in norm_dict)
        out_indices = tuple(
            l2g[b.indices[row]] if b.validity[row] else 0
            for row in range(len(b.indices))
        )
        remaps.append(
            BatchRemap(
                batch_id=b.batch_id,
                local_to_global=l2g,
                global_indices=out_indices,
                validity=tuple(b.validity),
                row_count=len(b.indices),
                null_count=sum(1 for v in b.validity if not v),
            )
        )

    stats = {
        "batch_count": len(batches),
        "total_rows": total_rows,
        "total_null_rows": total_nulls,
        "cardinality": cardinality,
        "index_width_bits": width,
        "sort_policy": SORT_POLICY,
    }

    return UnifyResult(
        global_dictionary=tuple(global_dictionary),
        global_value_type=value_type,
        index_width_bits=width,
        cardinality=cardinality,
        sort_policy=SORT_POLICY,
        batch_remaps=tuple(remaps),
        stats=stats,
    )


def _auto_width(cardinality: int) -> int:
    """Smallest unsigned width that fits the cardinality.

    Cardinality 0 picks uint8: an empty dictionary carries no value codes at
    all (every row must be NULL), and uint8 is the smallest canonical Arrow
    dictionary index type.
    """
    for width in AUTO_WIDTHS:
        if cardinality == 0 or cardinality <= WIDTH_CAPACITY[width]:
            return width
    # AUTO_WIDTHS ends at 32; reaching here means the hard ceiling should have
    # tripped first. Keep an explicit guard either way.
    raise CardinalityLimitError(
        "cardinality exceeds auto-selectable widths",
        details={"cardinality": cardinality},
    )

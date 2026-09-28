"""JSON/dict <-> kernel neutral objects adapter.

Responsibilities kept here, out of the kernel:

* request shape validation (lists/strings/ints at the wire level);
* per-value-type scalar coercion (e.g. JSON ``1`` -> double ``1.0``);
* opt-in canonicalization of *duplicate dictionary entries*: when
  ``dedupe_local_dictionary`` is true, duplicate values inside a local
  dictionary are rewritten to the code of their first occurrence (indices are
  remapped) and the duplicates are counted. The kernel itself rejects
  duplicates, so the merge never silently guesses which code wins.
"""
from __future__ import annotations

import math

from ..core.errors import (
    MalformedBatchError,
    NullDictionaryEntryError,
    ValueTypeMismatchError,
)
from ..core.kernel import BatchInput, UnifyResult


def _is_int_excluding_bool(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def coerce_scalar(value, value_type: str, *, batch_id: str, code: int):
    """Wire-level scalar coercion with classified errors (NULL rejected)."""
    if value is None:
        raise NullDictionaryEntryError(
            "NULL is not a legal dictionary value; encode NULL rows via the "
            "validity bitmap",
            details={"batch_id": batch_id, "code": code},
        )
    if value_type == "string":
        if not isinstance(value, str):
            raise ValueTypeMismatchError(
                "dictionary entry is not a string",
                details={"batch_id": batch_id, "code": code,
                         "actual_type": type(value).__name__},
            )
        return value
    if value_type == "bool":
        if not isinstance(value, bool):
            raise ValueTypeMismatchError(
                "dictionary entry is not a bool",
                details={"batch_id": batch_id, "code": code,
                         "actual_type": type(value).__name__},
            )
        return value
    if value_type == "int64":
        if not _is_int_excluding_bool(value):
            raise ValueTypeMismatchError(
                "dictionary entry is not an int64",
                details={"batch_id": batch_id, "code": code,
                         "actual_type": type(value).__name__},
            )
        if not (-(2**63) <= value <= 2**63 - 1):
            raise ValueTypeMismatchError(
                "integer outside int64 range",
                details={"batch_id": batch_id, "code": code, "value": value},
            )
        return value
    if value_type == "double":
        if isinstance(value, bool):
            raise ValueTypeMismatchError(
                "dictionary entry is not a double",
                details={"batch_id": batch_id, "code": code, "actual_type": "bool"},
            )
        if _is_int_excluding_bool(value):
            value = float(value)
        if not isinstance(value, float):
            raise ValueTypeMismatchError(
                "dictionary entry is not a double",
                details={"batch_id": batch_id, "code": code,
                         "actual_type": type(value).__name__},
            )
        if math.isnan(value):
            raise ValueTypeMismatchError(
                "NaN is not supported (breaks fixed ordering)",
                details={"batch_id": batch_id, "code": code},
            )
        return value
    # Unsupported types are the kernel's job to reject; passing through is fine.
    return value


def batch_from_dict(
    raw: dict,
    *,
    value_type: str,
    dedupe: bool,
) -> tuple[BatchInput, dict]:
    """Build a BatchInput from a decoded JSON object.

    Returns the batch plus a normalization report (duplicate handling stats).
    """
    if not isinstance(raw, dict):
        raise MalformedBatchError(
            "each batch must be an object",
            details={"actual_type": type(raw).__name__},
        )
    batch_id = raw.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id:
        raise MalformedBatchError("batch.batch_id must be a non-empty string")

    raw_dict = raw.get("dictionary")
    raw_indices = raw.get("indices")
    raw_validity = raw.get("validity", None)

    if not isinstance(raw_dict, list):
        raise MalformedBatchError(
            "batch.dictionary must be a list", details={"batch_id": batch_id}
        )
    if not isinstance(raw_indices, list):
        raise MalformedBatchError(
            "batch.indices must be a list", details={"batch_id": batch_id}
        )

    # Normalize values up front; NULL entries are rejected (they belong in the
    # bitmap, not in value space).
    norm_values = [
        coerce_scalar(v, value_type, batch_id=batch_id, code=i)
        for i, v in enumerate(raw_dict)
    ]

    # Duplicate-value handling within a local dictionary.
    first_code: dict = {}
    canonical: list = []          # canonical local code -> value
    code_map: list[int] = []      # original local code -> canonical local code
    duplicate_pairs: list[dict] = []
    for code, value in enumerate(norm_values):
        if value in first_code:
            code_map.append(first_code[value])
            duplicate_pairs.append(
                {"value_repr": repr(value),
                 "first_code": first_code[value], "duplicate_code": code}
            )
        else:
            first_code[value] = len(canonical)
            code_map.append(first_code[value])
            canonical.append(value)

    duplicates_removed = 0
    final_values = norm_values
    if duplicate_pairs:
        if not dedupe:
            # Surface via kernel-style classified error; build the same detail
            # the kernel would, so API behavior is identical.
            from ..core.errors import DuplicateValueInDictionaryError

            pair = duplicate_pairs[0]
            raise DuplicateValueInDictionaryError(
                "duplicate value in local dictionary; set "
                "dedupe_local_dictionary=true to canonicalize",
                details={"batch_id": batch_id, **pair},
            )
        duplicates_removed = len(duplicate_pairs)
        final_values = canonical

    # Validity defaults to all-valid; explicit bitmaps must align in length.
    if raw_validity is None:
        validity = [True] * len(raw_indices)
    else:
        if not isinstance(raw_validity, list) or len(raw_validity) != len(raw_indices):
            from ..core.errors import InvalidValidityError

            raise InvalidValidityError(
                "validity must be a list matching indices length",
                details={
                    "batch_id": batch_id,
                    "validity_len": len(raw_validity) if isinstance(raw_validity, list) else None,
                    "indices_len": len(raw_indices),
                },
            )
        bad = next((i for i, v in enumerate(raw_validity) if not isinstance(v, bool)), None)
        if bad is not None:
            from ..core.errors import InvalidValidityError

            raise InvalidValidityError(
                "validity entries must be booleans",
                details={"batch_id": batch_id, "row": bad,
                         "actual_type": type(raw_validity[bad]).__name__},
            )
        validity = list(raw_validity)

    # Rewrite indices to canonical codes after dedupe. Also validate index
    # shape here (range validation proper is the kernel's responsibility; the
    # code_map is only indexed when it is known to be in range).
    final_indices: list[int] = []
    for row, idx in enumerate(raw_indices):
        if not _is_int_excluding_bool(idx):
            from ..core.errors import InvalidIndexError

            raise InvalidIndexError(
                "index must be an integer",
                details={"batch_id": batch_id, "row": row,
                         "actual_type": type(idx).__name__},
            )
        if dedupe and duplicate_pairs and 0 <= idx < len(code_map):
            idx = code_map[idx]
        final_indices.append(idx)

    report = {
        "batch_id": batch_id,
        "duplicate_dictionary_entries": duplicates_removed,
        "duplicate_pairs": duplicate_pairs if dedupe else [],
    }
    batch = BatchInput(
        batch_id=batch_id,
        dictionary=final_values,
        indices=final_indices,
        validity=validity,
    )
    return batch, report


def result_to_dict(result: UnifyResult, *, normalization_reports: list[dict],
                   job_id: str | None = None) -> dict:
    """Serialize the kernel result to the JSON response envelope body."""
    return {
        "job_id": job_id,
        "global_dictionary": list(result.global_dictionary),
        "global_value_type": result.global_value_type,
        "index_width_bits": result.index_width_bits,
        "cardinality": result.cardinality,
        "sort_policy": result.sort_policy,
        "batch_remaps": [
            {
                "batch_id": r.batch_id,
                "local_to_global": list(r.local_to_global),
                "global_indices": list(r.global_indices),
                "validity": list(r.validity),
                "row_count": r.row_count,
                "null_count": r.null_count,
            }
            for r in result.batch_remaps
        ],
        "stats": result.stats,
        "normalization": normalization_reports,
    }

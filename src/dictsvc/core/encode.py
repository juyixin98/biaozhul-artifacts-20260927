"""Encoding kernel: validation, global merge, per-batch remap.

Merge semantics (the core correctness contract):

* the SAME value reached through DIFFERENT local codes (within a batch or
  across batches) collapses onto ONE global code;
* DIFFERENT values that happen to share a local code in different batches
  stay on DIFFERENT global codes (the global dictionary is keyed by
  ``(type, value)``, never by local code);
* NULL rows are governed only by the validity bitmap and are never
  dereferenced against the dictionary.
"""
from __future__ import annotations

from typing import Callable, Sequence

from .errors import (
    CardinalityOverflow,
    DictSvcError,
    DictionaryContainsNull,
    DuplicateBatchId,
    DuplicateDictionaryValue,
    IndexOutOfRange,
    UnsupportedValueType,
)
from .model import BatchInput, BatchRemap, BatchStats, GlobalEncoding
from .policy import (
    NULL_SENTINEL,
    SORT_POLICY,
    TYPE_RANK,
    WIDTHS,
    WIDTH_POLICIES,
    capacity,
)

EventSink = Callable[[dict], None]

INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1


def _value_type(v: object, batch_id: str | None = None,
                code: int | None = None) -> str:
    # NULL belongs only to the validity bitmap; it must never occupy an
    # ordinary dictionary value code -- enforced in the kernel itself so no
    # adapter can bypass it.
    if v is None:
        details = {}
        if code is not None:
            details["local_code"] = code
        raise DictionaryContainsNull(
            "a dictionary value is null; NULL rows must be expressed via "
            "the validity bitmap only",
            batch_id=batch_id, details=details)
    # bool is a subclass of int; dictionaries of booleans are out of scope.
    if isinstance(v, int) and not isinstance(v, bool):
        if not (INT64_MIN <= v <= INT64_MAX):
            raise UnsupportedValueType(
                f"integer {v} is outside the int64 range",
            )
        return "int64"
    if isinstance(v, str):
        return "utf8"
    raise UnsupportedValueType(
        f"unsupported dictionary value type: {type(v).__name__}",
        details={"python_type": type(v).__name__},
    )


def _validate_batch(b: BatchInput, on_duplicate_values: str) -> dict:
    """Validate one batch; return its derived per-batch info."""
    if not isinstance(b.batch_id, str) or not b.batch_id:
        raise DictSvcError("batch_id must be a non-empty string")

    declared = len(b.values)
    code_of: dict[tuple[str, object], int] = {}
    types: list[str] = []
    duplicate_declared = 0
    for code, v in enumerate(b.values):
        t = _value_type(v, b.batch_id, code)
        types.append(t)
        key = (t, v)
        if key in code_of:
            # Repeated dictionary item: same value bound to another local
            # code. Default policy merges; strict mode rejects explicitly.
            duplicate_declared += 1
            if on_duplicate_values == "error":
                raise DuplicateDictionaryValue(
                    f"value {v!r} is bound to both local codes "
                    f"{code_of[key]} and {code} in batch {b.batch_id!r}",
                    batch_id=b.batch_id,
                    details={"local_code_a": code_of[key],
                             "local_code_b": code, "value": v},
                )
        else:
            code_of[key] = code

    used_codes: set[int] = set()
    null_rows = 0
    for row, (idx, is_valid) in enumerate(zip(b.indices, b.valid)):
        if not is_valid:
            null_rows += 1
            # Independent bitmap semantics: do not touch the index here.
            continue
        if not isinstance(idx, int) or isinstance(idx, bool):
            raise IndexOutOfRange(
                f"row {row}: valid rows must carry an integer index",
                batch_id=b.batch_id, details={"row": row, "index": idx})
        if idx < 0 or idx >= declared:
            raise IndexOutOfRange(
                f"row {row}: local index {idx} is outside dictionary "
                f"[0, {declared}) in batch {b.batch_id!r}",
                batch_id=b.batch_id,
                details={"row": row, "index": idx,
                         "dictionary_size": declared})
        used_codes.add(idx)

    distinct = len(code_of)
    return {
        "types": types,
        "duplicate_declared": duplicate_declared,
        "used_codes": used_codes,
        "null_rows": null_rows,
        "distinct": distinct,
    }


def _decide_width(cardinality: int, target_width: int,
                  width_policy: str) -> int:
    """Apply the fixed unsigned-width ladder to the global cardinality."""
    if cardinality <= capacity(target_width):
        return target_width
    if width_policy == "reject":
        raise CardinalityOverflow(
            f"global cardinality {cardinality} exceeds capacity "
            f"{capacity(target_width)} of target width {target_width}-bit "
            f"and width_policy='reject'",
            details={"cardinality": cardinality,
                     "target_capacity": capacity(target_width),
                     "target_width": target_width,
                     "required_width": _min_width(cardinality)},
        )
    w = _min_width(cardinality)
    if w > WIDTHS[-1]:
        # 64-bit is the ladder ceiling; a larger dictionary is refused
        # rather than silently narrowed.
        raise CardinalityOverflow(
            f"global cardinality {cardinality} exceeds 64-bit capacity",
            details={"cardinality": cardinality,
                     "max_capacity": capacity(64)},
        )
    return w


def _min_width(cardinality: int) -> int:
    for w in WIDTHS:
        if cardinality <= capacity(w):
            return w
    return WIDTHS[-1] + 1  # signals "above the ladder"


def encode_run(
    batches: Sequence[BatchInput],
    *,
    target_width: int = 8,
    width_policy: str = "reject",
    sort_policy: str = SORT_POLICY,
    on_duplicate_values: str = "merge",
    event: EventSink | None = None,
) -> GlobalEncoding:
    """Validate, merge into the global dictionary and remap every batch."""
    sink = event or (lambda _e: None)
    sink({"step": "start", "batch_count": len(batches),
          "target_width": target_width, "width_policy": width_policy})

    if target_width not in WIDTHS:
        raise DictSvcError(
            f"target_width must be one of {WIDTHS}, got {target_width}")
    if width_policy not in WIDTH_POLICIES:
        raise DictSvcError(
            f"width_policy must be one of {WIDTH_POLICIES}, "
            f"got {width_policy!r}")
    if sort_policy != SORT_POLICY:
        raise DictSvcError(
            f"unsupported sort_policy {sort_policy!r}; the fixed policy is "
            f"{SORT_POLICY!r}")
    if on_duplicate_values not in ("merge", "error"):
        raise DictSvcError(
            "on_duplicate_values must be 'merge' or 'error'")

    seen_ids: set[str] = set()
    per_batch: dict[str, dict] = {}
    for b in batches:
        if b.batch_id in seen_ids:
            raise DuplicateBatchId(
                f"duplicate batch_id {b.batch_id!r} in one request",
                batch_id=b.batch_id)
        seen_ids.add(b.batch_id)
        info = _validate_batch(b, on_duplicate_values)
        per_batch[b.batch_id] = info
        sink({"step": "batch_validated", "batch_id": b.batch_id,
              "declared": len(b.values),
              "distinct": info["distinct"],
              "duplicate_declared": info["duplicate_declared"],
              "null_rows": info["null_rows"]})

    # Global merge keyed strictly by (type, value).
    global_keys: set[tuple[str, object]] = set()
    for b in batches:
        for code, v in enumerate(b.values):
            global_keys.add((_value_type(v, b.batch_id, code), v))
    ordered = sorted(
        global_keys,
        key=lambda kv: (TYPE_RANK[kv[0]],
                        kv[1] if kv[0] == "int64" else kv[1].encode("utf-8")),
    )
    cardinality = len(ordered)
    sink({"step": "global_merged", "cardinality": cardinality})

    global_index_width = _decide_width(cardinality, target_width,
                                       width_policy)
    sink({"step": "width_decided", "global_index_width": global_index_width})

    global_code = {key: code for code, key in enumerate(ordered)}

    remaps: list[BatchRemap] = []
    for b in batches:
        info = per_batch[b.batch_id]
        local_to_global = tuple(global_code[(info["types"][code], v)]
                                for code, v in enumerate(b.values))
        out_indices: list[int] = []
        for idx, is_valid in zip(b.indices, b.valid):
            if is_valid:
                out_indices.append(local_to_global[idx])
            else:
                out_indices.append(NULL_SENTINEL)
        # "Used" is defined at VALUE level: any valid row referencing a
        # local code bound to value v marks v as used. Unused declared
        # entries are distinct values no valid row ever referenced.
        used_values = {(info["types"][idx], b.values[idx])
                       for idx in info["used_codes"]}
        unused = info["distinct"] - len(used_values)
        stats = BatchStats(
            batch_id=b.batch_id,
            row_count=len(b.indices),
            null_rows=info["null_rows"],
            declared_entries=len(b.values),
            distinct_values=info["distinct"],
            used_entries=len(used_values),
            unused_declared=unused,
            duplicate_declared=info["duplicate_declared"],
        )
        remaps.append(BatchRemap(
            batch_id=b.batch_id,
            local_to_global=local_to_global,
            global_indices=tuple(out_indices),
            valid=tuple(b.valid),
            stats=stats,
        ))
        sink({"step": "batch_remapped", "batch_id": b.batch_id,
              "used_distinct_values": len(used_values),
              "unused_declared": unused})

    enc = GlobalEncoding(
        sort_policy=SORT_POLICY,
        width_policy=width_policy,
        target_width=target_width,
        global_index_width=global_index_width,
        global_types=tuple(t for t, _ in ordered),
        global_values=tuple(v for _, v in ordered),
        cardinality=cardinality,
        batches=tuple(remaps),
    )
    sink({"step": "done", "cardinality": cardinality,
          "global_index_width": global_index_width})
    return enc

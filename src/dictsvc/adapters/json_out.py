"""Serialize a GlobalEncoding to the stable JSON response shape."""
from __future__ import annotations

from ..core.decode import RoundtripReport
from ..core.model import GlobalEncoding


def _stats(s) -> dict:
    return {
        "row_count": s.row_count,
        "null_rows": s.null_rows,
        "declared_entries": s.declared_entries,
        "distinct_values": s.distinct_values,
        "used_entries": s.used_entries,
        "unused_declared": s.unused_declared,
        "duplicate_declared": s.duplicate_declared,
    }


def encode_to_dict(enc: GlobalEncoding, *, run_id: str,
                   report: RoundtripReport | None = None,
                   events: list[dict] | None = None) -> dict:
    resp = {
        "ok": True,
        "run_id": run_id,
        "policy": {
            "sort_policy": enc.sort_policy,
            "width_policy": enc.width_policy,
            "target_width": enc.target_width,
            "global_index_width": enc.global_index_width,
            "null_semantics": "validity bitmap only; NULL occupies no value",
        },
        "global_dictionary": {
            "cardinality": enc.cardinality,
            "entries": [
                {"global_code": code, "type": t, "value": v}
                for code, (t, v) in enumerate(
                    zip(enc.global_types, enc.global_values))
            ],
        },
        "batches": [
            {
                "batch_id": rb.batch_id,
                "local_to_global": list(rb.local_to_global),
                # Remapped rows: indices carry the NULL sentinel while the
                # independent bitmap states validity -- both are always
                # emitted, so consumers never infer nullness from a code.
                "global_indices": [
                    i if v else None
                    for i, v in zip(rb.global_indices, rb.valid)
                ],
                "valid": [bool(v) for v in rb.valid],
                "stats": _stats(rb.stats),
            }
            for rb in enc.batches
        ],
    }
    if report is not None:
        resp["verification"] = {
            "all_match": report.all_match,
            "checked_rows": report.checked_rows,
            "mismatches": [
                {"batch_id": m.batch_id, "row": m.row,
                 "expected": m.expected, "actual": m.actual}
                for m in report.mismatches
            ],
        }
    if events is not None:
        resp["events"] = events
    return resp
